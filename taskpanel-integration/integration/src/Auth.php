<?php
declare(strict_types=1);

namespace JarvisIntegration;

/**
 * Two identities on every request (guide §5.1, §9.1):
 *  - service identity: HMAC-SHA256 signature by a registered JARVIS key id
 *  - actor identity:   a delegated grant issued for one TaskPanel staff member
 * A valid service signature alone can never act as any staff member.
 */
final class Auth
{
    public function __construct(private \PDO $pdo, private array $config)
    {
    }

    /** @return array{client_id:string, actor_key:string, scopes:string[]} */
    public function verify(string $rawBody): array
    {
        $keyId = Http::header('X-Integration-Key-Id');
        $stamp = Http::header('X-Integration-Timestamp');
        $nonce = Http::header('X-Integration-Nonce');
        $grant = Http::header('X-Actor-Grant');
        $digest = strtolower(Http::header('X-Content-SHA256'));
        $signature = Http::header('X-Integration-Signature');

        if ($keyId === '' || $stamp === '' || $nonce === '' || $grant === '' || $digest === '' || $signature === '') {
            throw new IntegrationError(401, 'missing_auth', 'Missing integration authentication headers.');
        }
        $client = $this->config['clients'][$keyId] ?? null;
        if (!$client || ($client['environment'] ?? '') !== $this->config['environment']) {
            throw new IntegrationError(401, 'unknown_key', 'Unknown integration key.');
        }
        if (!ctype_digit($stamp) || abs(time() - (int)$stamp) > (int)$this->config['timestamp_tolerance_seconds']) {
            throw new IntegrationError(401, 'stale_timestamp', 'Request timestamp is outside the allowed window.');
        }
        if (!preg_match('/^[A-Za-z0-9-]{8,64}$/', $nonce)) {
            throw new IntegrationError(401, 'bad_nonce', 'Invalid nonce.');
        }
        if (!hash_equals(hash('sha256', $rawBody), $digest)) {
            throw new IntegrationError(401, 'digest_mismatch', 'Body digest does not match.');
        }
        $canonical = implode("\n", [
            Http::method(), Http::target(), $keyId, $stamp, $nonce, hash('sha256', $grant), $digest,
        ]);
        $expected = 'v1=' . hash_hmac('sha256', $canonical, (string)$client['secret']);
        if (!hash_equals($expected, $signature)) {
            throw new IntegrationError(401, 'bad_signature', 'Signature check failed.');
        }

        // Replay protection: a nonce is accepted once within the window (stored only after the signature verifies).
        $this->pdo->prepare('DELETE FROM taskpanel_integration_nonces WHERE expires_at < NOW()')->execute();
        try {
            $this->pdo->prepare('INSERT INTO taskpanel_integration_nonces (client_id, nonce, expires_at) VALUES (?, ?, DATE_ADD(NOW(), INTERVAL ? SECOND))')
                ->execute([$keyId, $nonce, 2 * (int)$this->config['timestamp_tolerance_seconds']]);
        } catch (\PDOException) {
            throw new IntegrationError(401, 'replayed_nonce', 'This request was already used.');
        }

        $stmt = $this->pdo->prepare(
            'SELECT staff_key, scopes FROM taskpanel_integration_grants
             WHERE grant_hash = ? AND client_id = ? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())'
        );
        $stmt->execute([hash('sha256', $grant), $keyId]);
        $row = $stmt->fetch(\PDO::FETCH_ASSOC);
        if (!$row) {
            throw new IntegrationError(401, 'invalid_grant', 'The actor grant is invalid, expired or revoked.');
        }
        $scopes = array_values(array_intersect(
            preg_split('/\s+/', trim((string)$row['scopes'])) ?: [],
            $client['scopes'] ?? []
        ));
        return ['client_id' => $keyId, 'actor_key' => (string)$row['staff_key'], 'scopes' => $scopes];
    }

    public static function requireScope(array $ctx, string $scope): void
    {
        if (!in_array($scope, $ctx['scopes'], true)) {
            throw new IntegrationError(403, 'scope_missing', "This grant does not allow {$scope}.");
        }
    }
}
