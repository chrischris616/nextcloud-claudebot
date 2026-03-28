<?php
\OCP\Util::addStyle('claudebot', 'admin');
\OCP\Util::addScript('claudebot', 'admin');
/** @var \OCP\IL10N $l */
?>

<div id="claudebot-settings" class="section">
    <h2><?php p($l->t('Claude Bot — Permissions')); ?></h2>
    <p class="settings-hint"><?php p($l->t('Manage which users and groups are allowed to interact with the Claude Talk Bot.')); ?></p>

    <h3><?php p($l->t('Allowed Users')); ?></h3>
    <div class="claudebot-add-row">
        <div class="claudebot-ac-wrapper">
            <input type="text" id="claudebot-user-input" placeholder="<?php p($l->t('Search user...')); ?>" autocomplete="off" />
            <div id="claudebot-user-dropdown" class="claudebot-ac-dropdown"></div>
        </div>
        <button id="claudebot-add-user" class="button primary"><?php p($l->t('Add')); ?></button>
    </div>
    <table id="claudebot-user-table" class="claudebot-table">
        <thead><tr><th><?php p($l->t('User')); ?></th><th><?php p($l->t('Added by')); ?></th><th><?php p($l->t('Date')); ?></th><th></th></tr></thead>
        <tbody></tbody>
    </table>

    <h3><?php p($l->t('Allowed Groups')); ?></h3>
    <div class="claudebot-add-row">
        <div class="claudebot-ac-wrapper">
            <input type="text" id="claudebot-group-input" placeholder="<?php p($l->t('Search group...')); ?>" autocomplete="off" />
            <div id="claudebot-group-dropdown" class="claudebot-ac-dropdown"></div>
        </div>
        <button id="claudebot-add-group" class="button primary"><?php p($l->t('Add')); ?></button>
    </div>
    <table id="claudebot-group-table" class="claudebot-table">
        <thead><tr><th><?php p($l->t('Group')); ?></th><th><?php p($l->t('Added by')); ?></th><th><?php p($l->t('Date')); ?></th><th></th></tr></thead>
        <tbody></tbody>
    </table>
</div>
