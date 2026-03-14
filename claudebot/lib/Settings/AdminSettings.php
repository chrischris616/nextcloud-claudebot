<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Settings;

use OCP\AppFramework\Http\TemplateResponse;
use OCP\Settings\ISettings;

class AdminSettings implements ISettings {
    public function getForm(): TemplateResponse {
        return new TemplateResponse('claudebot', 'admin');
    }

    public function getSection(): string {
        return 'claudebot';
    }

    public function getPriority(): int {
        return 50;
    }
}
