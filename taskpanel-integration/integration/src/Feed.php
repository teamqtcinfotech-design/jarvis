<?php
declare(strict_types=1);

namespace JarvisIntegration;

/**
 * Published change feed (guide §8.1-8.3) built by CHANGE DETECTION, because existing TaskPanel files
 * must not be edited: tasks touched since the last scan are re-read, and an event is appended when the
 * allowlisted snapshot actually changed. This captures web UI, cron, leave transfer and every other
 * mutation path without hooking them. Publication is serialized by one DB lock, so the cursor (feed seq)
 * can never skip an event. Rapid changes between two scans are coalesced into the latest state.
 */
final class Feed
{
    private const LOCK = 'jarvis_sync_publish';

    public function __construct(private \PDO $pdo, private TaskPanelAdapter $adapter, private array $config)
    {
    }

    private function state(string $name, ?string $default = null): ?string
    {
        $stmt = $this->pdo->prepare('SELECT value FROM taskpanel_sync_state WHERE name = ?');
        $stmt->execute([$name]);
        $value = $stmt->fetchColumn();
        return $value === false ? $default : (string)$value;
    }

    private function setState(string $name, string $value): void
    {
        $this->pdo->prepare('INSERT INTO taskpanel_sync_state (name, value) VALUES (?, ?) ON DUPLICATE KEY UPDATE value = VALUES(value)')
            ->execute([$name, $value]);
    }

    private function lock(int $waitSeconds = 0): bool
    {
        $stmt = $this->pdo->prepare('SELECT GET_LOCK(?, ?)');
        $stmt->execute([self::LOCK, $waitSeconds]);
        return (int)$stmt->fetchColumn() === 1;
    }

    private function unlock(): void
    {
        $this->pdo->prepare('SELECT RELEASE_LOCK(?)')->execute([self::LOCK]);
    }

    public function entity(int $taskId): array
    {
        $this->pdo->prepare('INSERT IGNORE INTO taskpanel_sync_entities (task_id, uuid) VALUES (?, ?)')
            ->execute([$taskId, uuid4()]);
        $stmt = $this->pdo->prepare('SELECT task_id, uuid, sync_revision, last_hash FROM taskpanel_sync_entities WHERE task_id = ?');
        $stmt->execute([$taskId]);
        return $stmt->fetch(\PDO::FETCH_ASSOC);
    }

    public function entityByUuid(string $uuid): ?array
    {
        $stmt = $this->pdo->prepare('SELECT task_id, uuid, sync_revision FROM taskpanel_sync_entities WHERE uuid = ?');
        $stmt->execute([$uuid]);
        return $stmt->fetch(\PDO::FETCH_ASSOC) ?: null;
    }

    /** Detect changes and publish (bounded, serialized). Called by GET /changes and by bin/sync-publish.php. */
    public function publish(bool $force = false): int
    {
        $minInterval = (int)($this->config['detect_min_interval_seconds'] ?? 3);
        $last = (int)$this->state('last_detect_at', '0');
        if (!$force && time() - $last < $minInterval) {
            return 0;
        }
        if (!$this->lock($force ? 10 : 0)) {
            return 0;
        }
        try {
            $this->setState('last_detect_at', (string)time());
            $watermark = $this->state('watermark', date('Y-m-d H:i:s', time() - 60));
            $scanStartedAt = date('Y-m-d H:i:s');
            $since = date('Y-m-d H:i:s', strtotime($watermark) - 10); // overlap for late commits; hashes dedupe
            $published = 0;
            foreach ($this->adapter->changedTaskIds($since) as $taskId) {
                $published += $this->publishTaskLocked($taskId, null, 'taskpanel') ? 1 : 0;
            }
            $this->setState('watermark', $scanStartedAt);
            $days = (int)($this->config['feed_retention_days'] ?? 30);
            $this->pdo->prepare('DELETE FROM taskpanel_sync_feed WHERE created_at < DATE_SUB(NOW(), INTERVAL ? DAY)')->execute([$days]);
            return $published;
        } finally {
            $this->unlock();
        }
    }

    /** Publish one task right after JARVIS created it (takes the lock itself). */
    public function publishTask(int $taskId, ?string $causationCommandId, string $origin): void
    {
        if (!$this->lock(5)) {
            return; // the next detection pass will publish it
        }
        try {
            $this->publishTaskLocked($taskId, $causationCommandId, $origin);
        } finally {
            $this->unlock();
        }
    }

    private function publishTaskLocked(int $taskId, ?string $causationCommandId, string $origin): bool
    {
        $snap = $this->adapter->snapshot($taskId);
        if ($snap === null) {
            return false;
        }
        $hash = hash('sha256', canonical_json($snap));
        $this->entity($taskId);
        $this->pdo->beginTransaction();
        try {
            $stmt = $this->pdo->prepare('SELECT uuid, sync_revision, last_hash FROM taskpanel_sync_entities WHERE task_id = ? FOR UPDATE');
            $stmt->execute([$taskId]);
            $entity = $stmt->fetch(\PDO::FETCH_ASSOC);
            if ($entity['last_hash'] === $hash) {
                $this->pdo->commit();
                return false;
            }
            $revision = (int)$entity['sync_revision'] + 1;
            $type = match (true) {
                $revision === 1 => 'task.created',
                $snap['status'] === 'done' => 'task.completed',
                $snap['status'] === 'waiting' => 'task.held',
                $snap['status'] === 'working' => 'task.started',
                default => 'task.updated',
            };
            $event = [
                'schema_version' => 1,
                'event_id' => uuid4(),
                'source_instance' => $this->config['instance_id'],
                'environment' => $this->config['environment'],
                'type' => $type,
                'task_uuid' => $entity['uuid'],
                'task_version' => $snap['version'],
                'sync_revision' => $revision,
                'origin' => $origin,
                'causation_command_id' => $causationCommandId,
                'occurred_at' => gmdate('Y-m-d\TH:i:s\Z'),
                'task' => $snap + ['uuid' => $entity['uuid'], 'sync_revision' => $revision],
            ];
            $this->pdo->prepare('UPDATE taskpanel_sync_entities SET sync_revision = ?, last_hash = ? WHERE task_id = ?')
                ->execute([$revision, $hash, $taskId]);
            $this->pdo->prepare('INSERT INTO taskpanel_sync_feed (event_id, environment, task_id, task_uuid, sync_revision, event_type, payload) VALUES (?, ?, ?, ?, ?, ?, ?)')
                ->execute([$event['event_id'], $event['environment'], $taskId, $entity['uuid'], $revision, $type,
                    json_encode($event, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)]);
            $this->pdo->commit();
            return true;
        } catch (\Throwable $e) {
            if ($this->pdo->inTransaction()) {
                $this->pdo->rollBack();
            }
            throw $e;
        }
    }

    public function highWater(): int
    {
        return (int)$this->pdo->query('SELECT COALESCE(MAX(seq), 0) FROM taskpanel_sync_feed')->fetchColumn();
    }

    public function encodeCursor(int $seq, array $actor): string
    {
        $raw = json_encode(['s' => $seq, 'e' => $this->config['environment'], 'a' => $actor['key']]);
        return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
    }

    private function decodeCursor(string $cursor, array $actor): int
    {
        $data = json_decode((string)base64_decode(strtr($cursor, '-_', '+/')), true);
        if (!is_array($data) || ($data['e'] ?? null) !== $this->config['environment'] || ($data['a'] ?? null) !== $actor['key']) {
            throw new IntegrationError(400, 'cursor_invalid', 'Cursor does not belong to this actor or environment.');
        }
        return (int)$data['s'];
    }

    /** GET /changes: published, actor-scoped events after the cursor. Empty cursor starts at "now". */
    public function changes(array $actor, string $cursor, int $limit): array
    {
        $this->publish();
        $now = gmdate('Y-m-d\TH:i:s\Z');
        if ($cursor === '') {
            return ['events' => [], 'next_cursor' => $this->encodeCursor($this->highWater(), $actor), 'has_more' => false, 'server_time' => $now];
        }
        $seq = $this->decodeCursor($cursor, $actor);
        $min = $this->pdo->query('SELECT MIN(seq) FROM taskpanel_sync_feed')->fetchColumn();
        if ($min !== null && $min !== false && $seq < (int)$min - 1) {
            throw new IntegrationError(410, 'cursor_expired', 'Change cursor expired; rebuild from snapshot.');
        }
        $stmt = $this->pdo->prepare(sprintf(
            'SELECT seq, payload FROM taskpanel_sync_feed WHERE seq > ? AND environment = ? ORDER BY seq LIMIT %d', $limit + 1));
        $stmt->execute([$seq, $this->config['environment']]);
        $rows = $stmt->fetchAll(\PDO::FETCH_ASSOC);
        $hasMore = count($rows) > $limit;
        $rows = array_slice($rows, 0, $limit);
        $events = [];
        $last = $seq;
        foreach ($rows as $row) {
            $last = (int)$row['seq'];
            $event = json_decode((string)$row['payload'], true);
            if ($event && $this->adapter->canRead($actor, $event['task'])) {
                $events[] = $event; // permission re-checked at read time
            }
        }
        return ['events' => $events, 'next_cursor' => $this->encodeCursor($last, $actor), 'has_more' => $hasMore, 'server_time' => $now];
    }
}
