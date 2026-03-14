<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Settings;

use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

class AdminSection implements IIconSection {
    public function __construct(
        private IURLGenerator $urlGenerator,
        private IL10N $l,
    ) {
    }

    public function getID(): string {
        return 'claudebot';
    }

    public function getName(): string {
        return $this->l->t('Claude Bot');
    }

    public function getPriority(): int {
        return 90;
    }

    public function getIcon(): string {
        return $this->urlGenerator->imagePath('claudebot', 'app.svg');
    }
}
