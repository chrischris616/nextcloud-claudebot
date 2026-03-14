<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\AppInfo;

use OCP\AppFramework\App;

class Application extends App {
    public const APP_ID = 'claudebot';

    public function __construct(array $urlParams = []) {
        parent::__construct(self::APP_ID, $urlParams);
    }
}
