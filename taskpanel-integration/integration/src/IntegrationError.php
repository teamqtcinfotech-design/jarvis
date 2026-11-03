<?php
declare(strict_types=1);

namespace JarvisIntegration;

/** Safe-to-return error with a stable code (guide §6.4). Never carries traces or secrets. */
final class IntegrationError extends \RuntimeException
{
    public function __construct(
        public readonly int $status,
        public readonly string $errorCode,
        string $message,
        public readonly array $extra = [],
    ) {
        parent::__construct($message);
    }
}
