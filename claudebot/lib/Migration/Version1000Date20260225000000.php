<?php

declare(strict_types=1);

namespace OCA\ClaudeBot\Migration;

use Closure;
use OCP\DB\ISchemaWrapper;
use OCP\DB\Types;
use OCP\Migration\IOutput;
use OCP\Migration\SimpleMigrationStep;

class Version1000Date20260225000000 extends SimpleMigrationStep {
    public function changeSchema(IOutput $output, Closure $schemaClosure, array $options): ?ISchemaWrapper {
        /** @var ISchemaWrapper $schema */
        $schema = $schemaClosure();

        if (!$schema->hasTable('claudebot_permissions')) {
            $table = $schema->createTable('claudebot_permissions');

            $table->addColumn('id', Types::BIGINT, [
                'autoincrement' => true,
                'notnull' => true,
                'unsigned' => true,
            ]);
            $table->addColumn('type', Types::STRING, [
                'notnull' => true,
                'length' => 16,
            ]);
            $table->addColumn('target', Types::STRING, [
                'notnull' => true,
                'length' => 64,
            ]);
            $table->addColumn('added_by', Types::STRING, [
                'notnull' => true,
                'length' => 64,
            ]);
            $table->addColumn('added_at', Types::BIGINT, [
                'notnull' => true,
                'unsigned' => true,
            ]);

            $table->setPrimaryKey(['id']);
            $table->addUniqueIndex(['type', 'target'], 'claudebot_perm_type_target');
        }

        return $schema;
    }
}
