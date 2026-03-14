<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Db;

use OCP\AppFramework\Db\QBMapper;
use OCP\IDBConnection;

/**
 * @extends QBMapper<Permission>
 */
class PermissionMapper extends QBMapper {
    public function __construct(IDBConnection $db) {
        parent::__construct($db, 'claudebot_permissions', Permission::class);
    }

    /**
     * @return Permission[]
     */
    public function findAll(): array {
        $qb = $this->db->getQueryBuilder();
        $qb->select('*')
            ->from($this->getTableName())
            ->orderBy('type')
            ->addOrderBy('target');
        return $this->findEntities($qb);
    }

    /**
     * Find permissions by type (user or group).
     * @return Permission[]
     */
    public function findByType(string $type): array {
        $qb = $this->db->getQueryBuilder();
        $qb->select('*')
            ->from($this->getTableName())
            ->where($qb->expr()->eq('type', $qb->createNamedParameter($type)))
            ->orderBy('target');
        return $this->findEntities($qb);
    }

    /**
     * Check if a direct user permission exists.
     */
    public function hasUserPermission(string $userId): bool {
        $qb = $this->db->getQueryBuilder();
        $qb->select($qb->func()->count('*', 'cnt'))
            ->from($this->getTableName())
            ->where($qb->expr()->eq('type', $qb->createNamedParameter('user')))
            ->andWhere($qb->expr()->eq('target', $qb->createNamedParameter($userId)));
        $result = $qb->executeQuery();
        $count = (int) $result->fetchOne();
        $result->closeCursor();
        return $count > 0;
    }

    /**
     * Get all group names that have permission.
     * @return string[]
     */
    public function getPermittedGroups(): array {
        $qb = $this->db->getQueryBuilder();
        $qb->select('target')
            ->from($this->getTableName())
            ->where($qb->expr()->eq('type', $qb->createNamedParameter('group')));
        $result = $qb->executeQuery();
        $groups = [];
        while ($row = $result->fetch()) {
            $groups[] = $row['target'];
        }
        $result->closeCursor();
        return $groups;
    }

    /**
     * Check if type+target combination already exists.
     */
    public function exists(string $type, string $target): bool {
        $qb = $this->db->getQueryBuilder();
        $qb->select($qb->func()->count('*', 'cnt'))
            ->from($this->getTableName())
            ->where($qb->expr()->eq('type', $qb->createNamedParameter($type)))
            ->andWhere($qb->expr()->eq('target', $qb->createNamedParameter($target)));
        $result = $qb->executeQuery();
        $count = (int) $result->fetchOne();
        $result->closeCursor();
        return $count > 0;
    }

    /**
     * Find a permission by ID.
     * @throws \OCP\AppFramework\Db\DoesNotExistException
     */
    public function findById(int $id): Permission {
        $qb = $this->db->getQueryBuilder();
        $qb->select("*")
            ->from($this->getTableName())
            ->where($qb->expr()->eq("id", $qb->createNamedParameter($id)));
        return $this->findEntity($qb);
    }
}
