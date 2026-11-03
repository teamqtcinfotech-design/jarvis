<?php
declare(strict_types=1);

namespace JarvisIntegration;

final class Http
{
    public static string $correlationId = '';

    public static function method(): string
    {
        return strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
    }

    /** Route relative to this folder, e.g. "/v1/changes". */
    public static function route(): string
    {
        $path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
        $base = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '')), '/');
        if ($base !== '' && str_starts_with($path, $base)) {
            $path = substr($path, strlen($base));
        }
        return '/' . trim($path, '/');
    }

    /** Exact request target as received (path + query): this is what JARVIS signed. */
    public static function target(): string
    {
        return $_SERVER['REQUEST_URI'] ?? '/';
    }

    public static function header(string $name): string
    {
        $key = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
        return trim((string)($_SERVER[$key] ?? ''));
    }

    public static function rawBody(int $maxBytes): string
    {
        $length = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
        if ($length > $maxBytes) {
            throw new IntegrationError(413, 'body_too_large', 'Request body is too large.');
        }
        $raw = (string)file_get_contents('php://input', false, null, 0, $maxBytes + 1);
        if (strlen($raw) > $maxBytes) {
            throw new IntegrationError(413, 'body_too_large', 'Request body is too large.');
        }
        return $raw;
    }

    public static function jsonBody(string $raw): array
    {
        if ($raw === '') {
            return [];
        }
        $type = strtolower($_SERVER['CONTENT_TYPE'] ?? '');
        if (!str_starts_with($type, 'application/json')) {
            throw new IntegrationError(415, 'unsupported_media_type', 'Content-Type must be application/json.');
        }
        try {
            $data = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
        } catch (\JsonException) {
            throw new IntegrationError(400, 'invalid_json', 'Body is not valid JSON.');
        }
        if (!is_array($data)) {
            throw new IntegrationError(400, 'invalid_json', 'Body must be a JSON object.');
        }
        return $data;
    }

    public static function json(int $status, array $body): never
    {
        http_response_code($status);
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store');
        header('X-Correlation-Id: ' . self::$correlationId);
        echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        exit;
    }

    public static function error(IntegrationError $e): never
    {
        self::json($e->status, array_merge([
            'code' => $e->errorCode,
            'message' => $e->getMessage(),
            'retryable' => $e->status === 429 || $e->status >= 500,
            'correlation_id' => self::$correlationId,
        ], $e->extra));
    }
}
