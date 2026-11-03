<?php
/*
 * Fallback publisher for the change feed. Add to the EXISTING server cron (CLI, not a web URL):
 *   * * * * * /usr/bin/php /path/to/public_html/taskPanel/integration/bin/sync-publish.php >/dev/null 2>&1
 * GET /changes also publishes on demand (at most every few seconds), so cron only needs to run every minute.
 */
declare(strict_types=1);

namespace JarvisIntegration;

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
require dirname(__DIR__) . '/src/bootstrap.php';

$config = config();
if (empty($config['enabled'])) {
    exit(0);
}
$adapter = new TaskPanelAdapter($config);
$feed = new Feed($adapter->pdo(), $adapter, $config);
$count = $feed->publish(true);
echo date('c') . " published {$count} event(s)\n";
