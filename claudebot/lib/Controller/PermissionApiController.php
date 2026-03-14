<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Controller;

use OCA\ClaudeBot\Db\Permission;
use OCA\ClaudeBot\Db\PermissionMapper;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\OCSController;
use OCP\AppFramework\Http\DataResponse;
use OCP\IConfig;
use OCP\IGroupManager;
use OCP\IRequest;
use OCP\IUserSession;

class PermissionApiController extends OCSController {
    private const DEFAULT_BOT_USER = 'bot-claude';

    public function __construct(
        string $appName,
        IRequest $request,
        private PermissionMapper $mapper,
        private IGroupManager $groupManager,
        private IUserSession $userSession,
        private IConfig $config,
    ) {
        parent::__construct($appName, $request);
    }

    private function getBotUser(): string {
        return $this->config->getAppValue('claudebot', 'bot_user', self::DEFAULT_BOT_USER);
    }

    private function isAppAdmin(): bool {
        $user = $this->userSession->getUser();
        if ($user === null) {
            return false;
        }
        return $this->groupManager->isAdmin($user->getUID());
    }

    /**
     * List all permissions.
     */
    #[NoAdminRequired]
    public function index(): DataResponse {
        if (!$this->isAppAdmin()) {
            return new DataResponse(['message' => 'Forbidden'], Http::STATUS_FORBIDDEN);
        }
        $permissions = $this->mapper->findAll();
        return new DataResponse(array_map(fn($p) => $p->jsonSerialize(), $permissions));
    }

    /**
     * Add a permission.
     */
    #[NoAdminRequired]
    public function create(string $type, string $target): DataResponse {
        if (!$this->isAppAdmin()) {
            return new DataResponse(['message' => 'Forbidden'], Http::STATUS_FORBIDDEN);
        }
        if (!in_array($type, ['user', 'group'], true)) {
            return new DataResponse(['message' => 'Invalid type. Must be "user" or "group".'], Http::STATUS_BAD_REQUEST);
        }
        if (empty(trim($target))) {
            return new DataResponse(['message' => 'Target must not be empty.'], Http::STATUS_BAD_REQUEST);
        }

        if ($this->mapper->exists($type, $target)) {
            return new DataResponse(['message' => 'Permission already exists.'], Http::STATUS_CONFLICT);
        }

        $user = $this->userSession->getUser();
        $permission = new Permission();
        $permission->setType($type);
        $permission->setTarget($target);
        $permission->setAddedBy($user ? $user->getUID() : 'unknown');
        $permission->setAddedAt(time());

        $permission = $this->mapper->insert($permission);
        return new DataResponse($permission->jsonSerialize(), Http::STATUS_CREATED);
    }

    /**
     * Remove a permission.
     */
    #[NoAdminRequired]
    public function destroy(int $id): DataResponse {
        if (!$this->isAppAdmin()) {
            return new DataResponse(['message' => 'Forbidden'], Http::STATUS_FORBIDDEN);
        }
        try {
            $permission = $this->mapper->findById($id);
            $this->mapper->delete($permission);
            return new DataResponse(['status' => 'ok']);
        } catch (\OCP\AppFramework\Db\DoesNotExistException $e) {
            return new DataResponse(['message' => 'Not found'], Http::STATUS_NOT_FOUND);
        }
    }

    /**
     * Check if a user has permission. Accessible by the configured bot user and admins.
     */
    #[NoAdminRequired]
    public function check(string $userId): DataResponse {
        $currentUser = $this->userSession->getUser();
        if ($currentUser === null) {
            return new DataResponse(['allowed' => false, 'reason' => 'not authenticated'], Http::STATUS_FORBIDDEN);
        }

        $currentUid = $currentUser->getUID();
        if ($currentUid !== $this->getBotUser() && !$this->isAppAdmin()) {
            return new DataResponse(['allowed' => false, 'reason' => 'forbidden'], Http::STATUS_FORBIDDEN);
        }

        // 1. Direct user permission
        if ($this->mapper->hasUserPermission($userId)) {
            return new DataResponse(['allowed' => true, 'reason' => 'user']);
        }

        // 2. Group permission
        $permittedGroups = $this->mapper->getPermittedGroups();
        foreach ($permittedGroups as $groupId) {
            if ($this->groupManager->isInGroup($userId, $groupId)) {
                return new DataResponse(['allowed' => true, 'reason' => 'group', 'group' => $groupId]);
            }
        }

        return new DataResponse(['allowed' => false, 'reason' => 'no permission']);
    }
}
