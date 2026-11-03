<?php
/*
 * Issue / revoke / list delegated grants for JARVIS (run on the TaskPanel server by its admin):
 *
 *   php bin/grant.php issue  --client=django-production-01 --staff-key=<user_key> --label="MD" --days=30
 *   php bin/grant.php revoke --staff-key=<user_key>
 *   php bin/grant.php list
 *
 * The grant is printed ONCE; only its SHA-256 hash is stored. Paste it into JARVIS with
 * `python manage.py taskpanel_link md --actor-key <user_key>` (hidden prompt).
 */
declare(strict_types=1);

namespace JarvisIntegration;

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}
require dirname(__DIR__) . '/src/bootstrap.php';

$args = [];
foreach (array_slice($argv, 2) as $arg) {
    if (preg_match('/^--([a-z-]+)=(.*)$/', $arg, $m)) {
        $args[$m[1]] = $m[2];
    }
}
$action = $argv[1] ?? '';
$config = config();
$adapter = new TaskPanelAdapter($config);
$pdo = $adapter->pdo();

switch ($action) {
    case 'issue':
        $client = $args['client'] ?? '';
        $staffKey = $args['staff-key'] ?? '';
        if (!isset($config['clients'][$client]) || $staffKey === '') {
            fwrite(STDERR, "Need --client=<configured key id> and --staff-key=<TaskPanel user_key>\n");
            exit(1);
        }
        $actor = $adapter->loadActor($staffKey); // must be an active TaskPanel staff member
        $scopes = $args['scopes'] ?? 'directory.read tasks.read tasks.create';
        $days = max(1, (int)($args['days'] ?? 30));
        $grant = rtrim(strtr(base64_encode(random_bytes(32)), '+/', '-_'), '=');
        $pdo->prepare('INSERT INTO taskpanel_integration_grants (client_id, grant_hash, staff_key, label, scopes, expires_at) VALUES (?, ?, ?, ?, ?, DATE_ADD(NOW(), INTERVAL ? DAY))')
            ->execute([$client, hash('sha256', $grant), $staffKey, $args['label'] ?? $actor['name'], $scopes, $days]);
        echo "Grant for {$actor['name']} ({$staffKey}), scopes [{$scopes}], valid {$days} days:\n\n{$grant}\n\n";
        echo "Shown once. Give it to the JARVIS admin over a secure channel; do not email or chat it.\n";
        break;
    case 'revoke':
        $staffKey = $args['staff-key'] ?? '';
        $stmt = $pdo->prepare('UPDATE taskpanel_integration_grants SET revoked_at = NOW() WHERE staff_key = ? AND revoked_at IS NULL');
        $stmt->execute([$staffKey]);
        echo "Revoked {$stmt->rowCount()} grant(s) for {$staffKey}.\n";
        break;
    case 'list':
        foreach ($pdo->query('SELECT staff_key, label, client_id, scopes, expires_at, revoked_at FROM taskpanel_integration_grants ORDER BY id DESC LIMIT 50') as $row) {
            printf("%-20s %-20s %-24s %-40s expires %s %s\n", $row['staff_key'], $row['label'], $row['client_id'],
                $row['scopes'], $row['expires_at'] ?? '-', $row['revoked_at'] ? '(REVOKED)' : '');
        }
        break;
    default:
        fwrite(STDERR, "Usage: php bin/grant.php issue|revoke|list [--options]\n");
        exit(1);
}
