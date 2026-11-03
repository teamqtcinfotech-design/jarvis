<?php
/*
 * Read-only installation check. Writes nothing to TaskPanel tables.
 *   php bin/self-check.php
 */
declare(strict_types=1);

namespace JarvisIntegration;

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
require dirname(__DIR__) . '/src/bootstrap.php';

$ok = true;
$say = function (bool $pass, string $what, string $detail = '') use (&$ok) {
    $ok = $ok && $pass;
    echo ($pass ? '[ OK ] ' : '[FAIL] ') . $what . ($detail !== '' ? " — {$detail}" : '') . "\n";
};

$say(PHP_VERSION_ID >= 80200, 'PHP 8.2+', PHP_VERSION);
try {
    $config = config();
    $say(true, 'config.local.php loaded', "environment={$config['environment']} writes_enabled=" . var_export((bool)$config['writes_enabled'], true));
} catch (\Throwable $e) {
    $say(false, 'config.local.php', $e->getMessage());
    exit(1);
}
foreach ($config['clients'] as $id => $client) {
    $say(strlen((string)$client['secret']) >= 40 && !str_contains($client['secret'], 'REPLACE'), "client {$id} secret is set");
}

$adapter = new TaskPanelAdapter($config);
try {
    $pdo = $adapter->pdo();
    $say(true, 'TaskPanel database connection');
} catch (\Throwable $e) {
    $say(false, 'TaskPanel database connection', $e->getMessage());
    exit(1);
}

foreach (['taskpanel_integration_grants', 'taskpanel_integration_nonces', 'taskpanel_integration_commands',
             'taskpanel_sync_entities', 'taskpanel_sync_feed', 'taskpanel_sync_state'] as $table) {
    $exists = (bool)$pdo->query("SHOW TABLES LIKE " . $pdo->quote($table))->fetchColumn();
    $say($exists, "table {$table}", $exists ? '' : 'run migrations/001_jarvis_integration.sql');
}

$schema = $config['schema'];
$cols = [];
try {
    foreach ($pdo->query('SHOW COLUMNS FROM `' . $schema['task_table'] . '`') as $c) {
        $cols[] = $c['Field'];
    }
    $say(true, "task table {$schema['task_table']}", implode(', ', $cols));
} catch (\Throwable $e) {
    $say(false, "task table {$schema['task_table']}", 'update schema.task_table in config.local.php');
}
foreach (['task_id', 'task_title', 'task_status', 'task_priority', 'task_assignee', 'task_sender', 'task_category',
             'task_due', 'task_version', 'task_completed_at', 'task_updated_at'] as $key) {
    if ($cols) {
        $say(in_array($schema[$key], $cols, true), "column {$key} → {$schema[$key]}");
    }
}

foreach (['TaskPanel\\Services\\TaskService' => 'create', 'TaskPanel\\Services\\StaffService' => null] as $class => $method) {
    $exists = class_exists($class);
    $say($exists, "class {$class}");
    if ($exists) {
        foreach ((new \ReflectionClass($class))->getMethods(\ReflectionMethod::IS_PUBLIC) as $m) {
            $params = implode(', ', array_map(fn($p) => ($p->getType() ? $p->getType() . ' ' : '') . '$' . $p->getName(), $m->getParameters()));
            echo "         {$m->getName()}({$params})\n";
        }
        if ($method) {
            $say(method_exists($class, $method), "{$class}::{$method} exists");
        }
    }
}
try {
    $dir = $adapter->directory();
    $say(count($dir['staff']) > 0, 'directory loads', count($dir['staff']) . ' staff, ' . count($dir['categories']) . ' categories');
} catch (\Throwable $e) {
    $say(false, 'directory loads', $e->getMessage());
}
echo $ok ? "\nAll checks passed.\n" : "\nSome checks failed — send this output to the JARVIS developer.\n";
exit($ok ? 0 : 1);
