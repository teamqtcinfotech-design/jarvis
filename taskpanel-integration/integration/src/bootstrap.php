<?php
declare(strict_types=1);

namespace JarvisIntegration;

date_default_timezone_set('Asia/Kolkata');

require_once __DIR__ . '/IntegrationError.php';
require_once __DIR__ . '/Http.php';
require_once __DIR__ . '/Auth.php';
require_once __DIR__ . '/TaskPanelAdapter.php';
require_once __DIR__ . '/Feed.php';
require_once __DIR__ . '/Commands.php';

function config(): array
{
    static $config = null;
    if ($config === null) {
        $file = dirname(__DIR__) . '/config.local.php';
        if (!is_file($file)) {
            throw new IntegrationError(503, 'not_configured', 'Integration is not configured on this server.');
        }
        $config = require $file;
    }
    return $config;
}

function uuid4(): string
{
    $b = random_bytes(16);
    $b[6] = chr((ord($b[6]) & 0x0f) | 0x40);
    $b[8] = chr((ord($b[8]) & 0x3f) | 0x80);
    $h = bin2hex($b);
    return sprintf('%s-%s-%s-%s-%s', substr($h, 0, 8), substr($h, 8, 4), substr($h, 12, 4), substr($h, 16, 4), substr($h, 20));
}

/** Stable JSON for hashing: recursively sorted keys, unescaped unicode/slashes. */
function canonical_json(mixed $value): string
{
    $sort = function ($v) use (&$sort) {
        if (is_array($v)) {
            if (array_is_list($v)) {
                return array_map($sort, $v);
            }
            ksort($v);
            return array_map($sort, $v);
        }
        return $v;
    };
    return json_encode($sort($value), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
}
