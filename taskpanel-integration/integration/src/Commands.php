<?php
declare(strict_types=1);

namespace JarvisIntegration;

/**
 * Idempotent commands (guide §6, §7.3). Phase 1 supports task.create only.
 *
 * Uniqueness is (client, command_id) in SQL, bound to the actor and a canonical payload hash:
 *  - same id + same payload  -> the stored result (replayed: true), never a second task
 *  - same id + other payload -> 409
 * TaskService::create keeps its own transaction (existing code is not modified), so the command row is
 * reserved BEFORE calling it. If the process dies between TaskService's commit and our receipt update,
 * the row stays "processing" and retries get 503 instead of creating a duplicate; an operator resolves it.
 */
final class Commands
{
    private const UUID = '/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/';

    public function __construct(private \PDO $pdo, private TaskPanelAdapter $adapter, private Feed $feed, private array $config)
    {
    }

    /** @return array{0:int,1:array} */
    public function execute(array $ctx, array $actor, array $body): array
    {
        Auth::requireScope($ctx, 'tasks.create');
        $commandId = strtolower((string)($body['command_id'] ?? ''));
        if ((int)($body['schema_version'] ?? 0) !== 1 || !preg_match(self::UUID, $commandId)) {
            throw new IntegrationError(422, 'invalid_command', 'schema_version 1 and a UUID command_id are required.');
        }
        if (strtolower(Http::header('Idempotency-Key')) !== $commandId) {
            throw new IntegrationError(422, 'idempotency_mismatch', 'Idempotency-Key must equal command_id.');
        }
        if (($body['type'] ?? '') !== 'task.create' || !is_array($body['data'] ?? null)) {
            throw new IntegrationError(422, 'unsupported_command', 'Only task.create is enabled in this phase.');
        }
        if (empty($this->config['writes_enabled'])) {
            throw new IntegrationError(503, 'writes_disabled', 'Integration writes are switched off.');
        }
        $hash = hash('sha256', canonical_json(['type' => $body['type'], 'data' => $body['data']]));

        try {
            $this->pdo->prepare('INSERT INTO taskpanel_integration_commands (client_id, command_id, actor_key, type, payload_hash, state) VALUES (?, ?, ?, ?, ?, ?)')
                ->execute([$ctx['client_id'], $commandId, $actor['key'], $body['type'], $hash, 'processing']);
        } catch (\PDOException) {
            return $this->replay($ctx, $actor, $commandId, $hash);
        }

        try {
            $taskId = $this->adapter->createTask($actor, $body['data']);
        } catch (IntegrationError $e) {
            if ($e->status === 422) {
                $this->finish($ctx, $commandId, 'rejected', 422, ['code' => $e->errorCode, 'message' => $e->getMessage()], null);
            } elseif ($e->status === 501) {
                $this->pdo->prepare('DELETE FROM taskpanel_integration_commands WHERE client_id = ? AND command_id = ?')
                    ->execute([$ctx['client_id'], $commandId]);
            }
            throw $e;
        } catch (\Throwable $t) {
            error_log('[jarvis-integration] create failed for command ' . $commandId . ': ' . $t->getMessage());
            throw new IntegrationError(503, 'create_outcome_unknown', 'TaskPanel could not confirm the task; it will be checked before any retry.');
        }

        $this->feed->publishTask($taskId, $commandId, 'jarvis');
        $entity = $this->feed->entity($taskId);
        $snap = $this->adapter->snapshot($taskId) ?? [];
        $result = [
            'command_id' => $commandId,
            'state' => 'succeeded',
            'replayed' => false,
            'task' => [
                'uuid' => $entity['uuid'],
                'legacy_id' => 'T-' . $taskId,
                'status' => $snap['status'] ?? 'todo',
                'assignee_key' => $snap['assignee_key'] ?? ($body['data']['assignee_key'] ?? null),
                'priority' => $snap['priority'] ?? ($body['data']['priority'] ?? 'normal'),
                'version' => $snap['version'] ?? 1,
                'sync_revision' => (int)$entity['sync_revision'],
            ],
        ];
        $this->finish($ctx, $commandId, 'succeeded', 201, $result, $taskId);
        return [201, $result];
    }

    private function finish(array $ctx, string $commandId, string $state, int $status, array $result, ?int $taskId): void
    {
        $this->pdo->prepare('UPDATE taskpanel_integration_commands SET state = ?, http_status = ?, result = ?, task_id = ? WHERE client_id = ? AND command_id = ?')
            ->execute([$state, $status, json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), $taskId, $ctx['client_id'], $commandId]);
    }

    /** @return array{0:int,1:array} */
    private function replay(array $ctx, array $actor, string $commandId, string $hash): array
    {
        $row = $this->row($ctx, $commandId);
        if (!$row || $row['actor_key'] !== $actor['key']) {
            throw new IntegrationError(409, 'command_conflict', 'This command id belongs to another request.');
        }
        if (!hash_equals($row['payload_hash'], $hash)) {
            throw new IntegrationError(409, 'idempotency_conflict', 'This command id was already used with different data.');
        }
        $result = json_decode((string)$row['result'], true) ?: [];
        if ($row['state'] === 'succeeded') {
            return [200, ['replayed' => true] + $result];
        }
        if ($row['state'] === 'rejected') {
            throw new IntegrationError(422, (string)($result['code'] ?? 'rejected'), (string)($result['message'] ?? 'Rejected.'));
        }
        $age = time() - strtotime((string)$row['created_at']);
        throw new IntegrationError(503, $age > 120 ? 'outcome_needs_review' : 'command_in_progress',
            'This command is still being processed. Retry later with the same command id.');
    }

    private function row(array $ctx, string $commandId): ?array
    {
        $stmt = $this->pdo->prepare('SELECT * FROM taskpanel_integration_commands WHERE client_id = ? AND command_id = ?');
        $stmt->execute([$ctx['client_id'], $commandId]);
        return $stmt->fetch(\PDO::FETCH_ASSOC) ?: null;
    }

    /** GET /commands/{id}: resolve a timeout or lost response. */
    public function status(array $ctx, array $actor, string $commandId): array
    {
        Auth::requireScope($ctx, 'tasks.read');
        $row = $this->row($ctx, strtolower($commandId));
        if (!$row || $row['actor_key'] !== $actor['key']) {
            throw new IntegrationError(404, 'command_not_found', 'No record of this command.');
        }
        $result = json_decode((string)$row['result'], true) ?: [];
        if ($row['state'] === 'succeeded') {
            return ['replayed' => true] + $result;
        }
        if ($row['state'] === 'rejected') {
            return ['command_id' => $commandId, 'state' => 'rejected'] + $result;
        }
        return ['command_id' => $commandId, 'state' => 'processing'];
    }
}
