<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Db;

use OCP\AppFramework\Db\Entity;

/**
 * @method string getType()
 * @method void setType(string $type)
 * @method string getTarget()
 * @method void setTarget(string $target)
 * @method string getAddedBy()
 * @method void setAddedBy(string $addedBy)
 * @method int getAddedAt()
 * @method void setAddedAt(int $addedAt)
 */
class Permission extends Entity {
    protected string $type = '';
    protected string $target = '';
    protected string $addedBy = '';
    protected int $addedAt = 0;

    public function __construct() {
        $this->addType('type', 'string');
        $this->addType('target', 'string');
        $this->addType('addedBy', 'string');
        $this->addType('addedAt', 'integer');
    }

    public function jsonSerialize(): array {
        return [
            'id' => $this->id,
            'type' => $this->type,
            'target' => $this->target,
            'addedBy' => $this->addedBy,
            'addedAt' => $this->addedAt,
        ];
    }
}
