<?php
declare(strict_types=1);

namespace JarvisIntegration;

/**
 * The ONLY file that touches TaskPanel internals.
 *
 *  - Writes go exclusively through TaskPanel's existing TaskService (rules, activity, notifications,
 *    leave/department checks all stay in force). This file never INSERTs/UPDATEs TaskPanel task tables.
 *  - Reads use TaskPanel's OWN database with the column mapping in config.local.php ('schema').
 *
 * Lines marked "VERIFY" depend on TaskPanel source that was not available when this was written
 * (class names, method signatures). Until verified, those paths return 501 "adapter_not_wired"
 * instead of guessing. Run `php bin/self-check.php` on the server to see what is wired.
 */
final class TaskPanelAdapter
{
    private ?\PDO $pdo = null;
    private bool $booted = false;
    private ?array $directoryCache = null;

    public function __construct(private array $config)
    {
    }

    public static function taskPanelRoot(): string
    {
        return dirname(__DIR__, 2); // .../taskPanel
    }

    /** Load TaskPanel's classes the same way its CLI cron.php does (no browser session). */
    public function boot(): void
    {
        if ($this->booted) {
            return;
        }
        $autoload = self::taskPanelRoot() . '/vendor/autoload.php';
        if (is_file($autoload)) {
            require_once $autoload;
        }
        // VERIFY: if cron.php requires bootstrap/app.php for config/DB setup, require it here too,
        // provided it does not dispatch a request or start a browser session.
        $this->booted = true;
    }

    public function pdo(): \PDO
    {
        if ($this->pdo) {
            return $this->pdo;
        }
        $this->boot();
        // VERIFY: TaskPanel's own-database accessor (guide §1.1 names Database::own()).
        foreach (['TaskPanel\\Support\\Database', 'TaskPanel\\Database', 'TaskPanel\\Core\\Database'] as $class) {
            if (class_exists($class) && method_exists($class, 'own')) {
                $pdo = $class::own();
                if ($pdo instanceof \PDO) {
                    return $this->pdo = $pdo;
                }
            }
        }
        $db = $this->config['db'] ?? null; // optional fallback: TaskPanel DB only, never the main software DB
        if (!$db) {
            throw new IntegrationError(501, 'adapter_not_wired', 'TaskPanel database accessor not wired yet.');
        }
        $this->pdo = new \PDO($db['dsn'], $db['user'], $db['pass'], [
            \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
            \PDO::ATTR_DEFAULT_FETCH_MODE => \PDO::FETCH_ASSOC,
        ]);
        return $this->pdo;
    }

    // ------------------------------------------------------------------ identity + directory
    /** Reload the actor on EVERY request so deactivation/role changes apply immediately (guide §5.2 step 6). */
    public function loadActor(string $staffKey): array
    {
        foreach ($this->directory()['staff'] as $person) {
            if ($person['key'] === $staffKey) {
                if (!$person['active']) {
                    throw new IntegrationError(403, 'actor_inactive', 'This staff account is not active.');
                }
                return $person;
            }
        }
        throw new IntegrationError(403, 'actor_unknown', 'This staff account is not in TaskPanel.');
    }

    /** Staff (immutable key, name, department, active, leave) and task categories. */
    public function directory(): array
    {
        if ($this->directoryCache !== null) {
            return $this->directoryCache;
        }
        $this->boot();
        // VERIFY: StaffService (staff + assignment departments), LeaveService (on leave today),
        // and where categories live (config/master.json per guide §17).
        $staffService = 'TaskPanel\\Services\\StaffService';
        if (!class_exists($staffService)) {
            throw new IntegrationError(501, 'adapter_not_wired', 'StaffService not wired yet.');
        }
        $staff = [];
        foreach ($this->callStaffList($staffService) as $row) {
            $staff[] = [
                'key' => (string)($row['user_key'] ?? $row['key'] ?? ''),
                'name' => (string)($row['name'] ?? ''),
                'department' => (string)($row['department'] ?? ''),
                'designation' => (string)($row['designation'] ?? ''),
                'active' => (bool)($row['active'] ?? true),
                'on_leave' => (bool)($row['on_leave'] ?? false),
                'leave_until' => $row['leave_until'] ?? null,
            ];
        }
        return $this->directoryCache = [
            'revision' => substr(hash('sha256', canonical_json($staff)), 0, 16),
            'staff' => array_values(array_filter($staff, fn($s) => $s['key'] !== '' && $s['name'] !== '')),
            'categories' => $this->categories(),
        ];
    }

    private function callStaffList(string $class): array
    {
        // VERIFY: real method name/signature, e.g. (new StaffService())->assignable() or ::all().
        foreach (['assignable', 'directory', 'all', 'list'] as $method) {
            if (method_exists($class, $method)) {
                $result = (new \ReflectionMethod($class, $method))->isStatic() ? $class::$method() : (new $class())->$method();
                return is_array($result) ? $result : [];
            }
        }
        throw new IntegrationError(501, 'adapter_not_wired', 'StaffService listing method not wired yet.');
    }

    private function categories(): array
    {
        $file = self::taskPanelRoot() . '/config/master.json';
        if (!is_file($file)) {
            return [['key' => 'general', 'name' => 'General', 'department' => '', 'active' => true]];
        }
        $master = json_decode((string)file_get_contents($file), true) ?: [];
        $out = [];
        // VERIFY: the real structure of config/master.json categories.
        foreach (($master['categories'] ?? []) as $key => $cat) {
            $out[] = [
                'key' => (string)($cat['key'] ?? $key),
                'name' => (string)($cat['name'] ?? $cat['label'] ?? $key),
                'department' => (string)($cat['department'] ?? ''),
                'active' => (bool)($cat['active'] ?? true),
            ];
        }
        return $out ?: [['key' => 'general', 'name' => 'General', 'department' => '', 'active' => true]];
    }

    // ------------------------------------------------------------------ validation + create
    /** Mirror TaskPanel's documented creation checks without writing anything (guide §1.3). */
    public function validateCreate(array $actor, array $data): array
    {
        $title = trim((string)($data['title'] ?? ''));
        if ($title === '' || mb_strlen($title) > 500) {
            throw new IntegrationError(422, 'title_invalid', 'A title of up to 500 characters is required.');
        }
        $dir = $this->directory();
        $person = null;
        foreach ($dir['staff'] as $s) {
            if ($s['key'] === ($data['assignee_key'] ?? null)) {
                $person = $s;
            }
        }
        if (!$person || !$person['active']) {
            throw new IntegrationError(422, 'recipient_not_found', 'That staff member is not in TaskPanel.');
        }
        if ($person['on_leave']) {
            throw new IntegrationError(422, 'recipient_on_leave', "{$person['name']} is on leave.");
        }
        $category = null;
        foreach ($dir['categories'] as $c) {
            if ($c['key'] === ($data['category_key'] ?? null) && $c['active']) {
                $category = $c;
            }
        }
        if (!$category) {
            throw new IntegrationError(422, 'category_unknown', 'Choose a valid task category.');
        }
        if ($category['department'] !== '' && $category['department'] !== $person['department']) {
            throw new IntegrationError(422, 'category_recipient_conflict',
                "{$category['name']} tasks go to {$category['department']}, but {$person['name']} is in {$person['department']}.");
        }
        if (!in_array($data['priority'] ?? '', ['normal', 'high', 'urgent'], true)) {
            throw new IntegrationError(422, 'priority_invalid', 'Priority must be normal, high or urgent.');
        }
        if (!empty($data['due_at']) && strtotime((string)$data['due_at']) < time()) {
            throw new IntegrationError(422, 'due_in_past', 'That time has already passed.');
        }
        return $data;
    }

    /** Create the task through TaskPanel's own TaskService. Returns the TaskPanel task id. */
    public function createTask(array $actor, array $data): int
    {
        $this->validateCreate($actor, $data);
        $dueTs = !empty($data['due_at']) ? strtotime((string)$data['due_at'])
            : (!empty($data['due_date']) ? strtotime($data['due_date'] . ' 18:30:00') : null);
        // Field names below are the documented browser contract (guide §6.2 adapter mapping).
        $input = [
            'cat' => $data['category_key'],
            'title' => $data['title'],
            'detail' => $data['description'],
            'to' => $data['assignee_key'],
            'pri' => $data['priority'],
            'manual' => true,
            'dueKind' => $dueTs ? 'custom' : 'none',
            'dueDate' => $dueTs ? date('Y-m-d', $dueTs) : null,
            'dueTime' => $dueTs ? date('H:i', $dueTs) : null,
            'remarks' => (string)($data['remarks'] ?? ''),
        ];
        $this->boot();
        $class = 'TaskPanel\\Services\\TaskService';
        if (!class_exists($class) || !method_exists($class, 'create')) {
            throw new IntegrationError(501, 'adapter_not_wired', 'TaskService::create not wired yet.');
        }
        // VERIFY: how TaskService receives the acting staff member. The browser path takes it from
        // SessionBridge; here it must be built from the verified grant ($actor), never from request JSON.
        $service = new $class();
        $result = $service->create($input, $actor);
        $id = is_array($result) ? ($result['id'] ?? $result['task_id'] ?? null) : $result;
        if (is_string($id) && str_starts_with($id, 'T-')) {
            $id = substr($id, 2);
        }
        if (!is_numeric($id)) {
            throw new IntegrationError(502, 'create_unconfirmed', 'TaskPanel did not return a task id.');
        }
        return (int)$id;
    }

    // ------------------------------------------------------------------ reads for snapshots + feed
    private function s(string $key): string
    {
        $col = (string)($this->config['schema'][$key] ?? '');
        if ($col !== '' && !preg_match('/^[A-Za-z0-9_]+$/', $col)) {
            throw new IntegrationError(500, 'bad_schema_config', 'Invalid schema mapping.');
        }
        return $col;
    }

    /** Allowlisted task fields only (guide §2.3: no private notes, client data or attachments). */
    public function snapshot(int $taskId): ?array
    {
        $t = $this->s('task_table');
        $hold = $this->s('task_hold_reason');
        $sql = sprintf(
            'SELECT %s AS id, %s AS title, %s AS status, %s AS priority, %s AS assignee_key, %s AS sender_key,
                    %s AS category_key, %s AS due_at, %s AS version, %s AS completed_at, %s AS hold_reason
             FROM %s WHERE %s = ?',
            $this->s('task_id'), $this->s('task_title'), $this->s('task_status'), $this->s('task_priority'),
            $this->s('task_assignee'), $this->s('task_sender'), $this->s('task_category'), $this->s('task_due'),
            $this->s('task_version'), $this->s('task_completed_at'), $hold !== '' ? $hold : "''",
            $t, $this->s('task_id')
        );
        $stmt = $this->pdo()->prepare($sql);
        $stmt->execute([$taskId]);
        $row = $stmt->fetch(\PDO::FETCH_ASSOC);
        if (!$row) {
            return null;
        }
        $toIso = fn($v) => $v ? date(DATE_ATOM, strtotime((string)$v)) : null;
        return [
            'legacy_id' => 'T-' . $row['id'],
            'title' => (string)$row['title'],
            'status' => (string)$row['status'],
            'priority' => (string)$row['priority'],
            'assignee_key' => (string)$row['assignee_key'],
            'sender_key' => (string)$row['sender_key'],
            'category_key' => (string)$row['category_key'],
            'due_at' => $toIso($row['due_at']),
            'version' => (int)$row['version'],
            'completed_at' => $toIso($row['completed_at']),
            'hold_reason' => (string)$row['hold_reason'],
        ];
    }

    /** Sender, assignee — VERIFY: also admins/viewers if TaskPanel lets them read others' tasks. */
    public function canRead(array $actor, array $snap): bool
    {
        return $snap['assignee_key'] === $actor['key'] || $snap['sender_key'] === $actor['key'];
    }

    /** Task ids changed since $since (task rows touched or new activity rows, e.g. notes). */
    public function changedTaskIds(string $since, int $limit = 500): array
    {
        $ids = [];
        $stmt = $this->pdo()->prepare(sprintf('SELECT %s FROM %s WHERE %s >= ? ORDER BY %s LIMIT %d',
            $this->s('task_id'), $this->s('task_table'), $this->s('task_updated_at'), $this->s('task_updated_at'), $limit));
        $stmt->execute([$since]);
        foreach ($stmt->fetchAll(\PDO::FETCH_COLUMN) as $id) {
            $ids[(int)$id] = true;
        }
        if ($this->s('activity_table') !== '') {
            $stmt = $this->pdo()->prepare(sprintf('SELECT DISTINCT %s FROM %s WHERE %s >= ? LIMIT %d',
                $this->s('activity_task_id'), $this->s('activity_table'), $this->s('activity_created'), $limit));
            $stmt->execute([$since]);
            foreach ($stmt->fetchAll(\PDO::FETCH_COLUMN) as $id) {
                $ids[(int)$id] = true;
            }
        }
        return array_keys($ids);
    }

    /** Page of task ids the actor sends or receives, for snapshots. */
    public function taskIdsForActor(array $actor, int $afterId, int $limit): array
    {
        $stmt = $this->pdo()->prepare(sprintf(
            'SELECT %1$s FROM %2$s WHERE %1$s > ? AND (%3$s = ? OR %4$s = ?) ORDER BY %1$s LIMIT %5$d',
            $this->s('task_id'), $this->s('task_table'), $this->s('task_assignee'), $this->s('task_sender'), $limit));
        $stmt->execute([$afterId, $actor['key'], $actor['key']]);
        return array_map('intval', $stmt->fetchAll(\PDO::FETCH_COLUMN));
    }
}
