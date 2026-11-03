<?php
/*
 * JARVIS integration entry point: /taskPanel/integration/v1/...
 * NEW file. TaskPanel's browser entry point, session and CSRF checks are untouched.
 */
declare(strict_types=1);

namespace JarvisIntegration;

require __DIR__ . '/src/bootstrap.php';

Http::$correlationId = bin2hex(random_bytes(8));

try {
    $route = Http::route();
    $method = Http::method();

    if ($route === '/v1/health' && $method === 'GET') {
        Http::json(200, ['status' => 'ok', 'schema_version' => 1]); // minimal public output
    }

    $config = config();
    if (empty($config['enabled'])) {
        throw new IntegrationError(503, 'disabled', 'Integration is switched off.');
    }
    $raw = Http::rawBody((int)($config['max_body_bytes'] ?? 65536));
    $adapter = new TaskPanelAdapter($config);
    $pdo = $adapter->pdo();
    $ctx = (new Auth($pdo, $config))->verify($raw);
    $actor = $adapter->loadActor($ctx['actor_key']); // live re-check: inactive staff are refused immediately
    $feed = new Feed($pdo, $adapter, $config);
    $commands = new Commands($pdo, $adapter, $feed, $config);

    if ($route === '/v1/directory' && $method === 'GET') {
        Auth::requireScope($ctx, 'directory.read');
        Http::json(200, $adapter->directory());
    }

    if ($route === '/v1/drafts/validate' && $method === 'POST') {
        Auth::requireScope($ctx, 'tasks.create');
        $body = Http::jsonBody($raw);
        $normalized = $adapter->validateCreate($actor, (array)($body['data'] ?? []));
        Http::json(200, ['valid' => true, 'warnings' => [], 'normalized' => $normalized]);
    }

    if ($route === '/v1/commands' && $method === 'POST') {
        [$status, $result] = $commands->execute($ctx, $actor, Http::jsonBody($raw));
        Http::json($status, $result);
    }

    if (preg_match('#^/v1/commands/([0-9a-fA-F-]{36})$#', $route, $m) && $method === 'GET') {
        Http::json(200, $commands->status($ctx, $actor, $m[1]));
    }

    if ($route === '/v1/tasks/snapshot' && $method === 'GET') {
        Auth::requireScope($ctx, 'tasks.read');
        $after = (int)($_GET['page'] ?? 0);
        $ids = $adapter->taskIdsForActor($actor, $after, 100);
        $tasks = [];
        foreach ($ids as $id) {
            $snap = $adapter->snapshot($id);
            if ($snap) {
                $entity = $feed->entity($id);
                $tasks[] = $snap + ['uuid' => $entity['uuid'], 'sync_revision' => (int)$entity['sync_revision']];
            }
        }
        Http::json(200, [
            'tasks' => $tasks,
            'next_page' => count($ids) === 100 ? (string)end($ids) : null,
            'high_water_cursor' => $feed->encodeCursor($feed->highWater(), $actor),
        ]);
    }

    if (preg_match('#^/v1/tasks/([0-9a-fA-F-]{36})$#', $route, $m) && $method === 'GET') {
        Auth::requireScope($ctx, 'tasks.read');
        $entity = $feed->entityByUuid(strtolower($m[1]));
        $snap = $entity ? $adapter->snapshot((int)$entity['task_id']) : null;
        if (!$snap || !$adapter->canRead($actor, $snap)) {
            throw new IntegrationError(404, 'task_not_found', 'Task not found.'); // never reveal others' tasks
        }
        Http::json(200, ['task' => $snap + ['uuid' => $entity['uuid'], 'sync_revision' => (int)$entity['sync_revision']]]);
    }

    if ($route === '/v1/changes' && $method === 'GET') {
        Auth::requireScope($ctx, 'tasks.read');
        $limit = max(1, min((int)($_GET['limit'] ?? $config['feed_page_default'] ?? 100), (int)($config['feed_page_max'] ?? 500)));
        Http::json(200, $feed->changes($actor, (string)($_GET['cursor'] ?? ''), $limit));
    }

    throw new IntegrationError(404, 'not_found', 'Unknown integration route.');
} catch (IntegrationError $e) {
    Http::error($e);
} catch (\Throwable $t) {
    error_log('[jarvis-integration ' . Http::$correlationId . '] ' . get_class($t) . ': ' . $t->getMessage());
    Http::json(500, ['code' => 'server_error', 'message' => 'Internal error.', 'retryable' => true,
        'correlation_id' => Http::$correlationId]);
}
