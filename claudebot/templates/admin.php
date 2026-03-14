<?php
\OCP\Util::addStyle('claudebot', 'admin');
\OCP\Util::addScript('claudebot', 'admin');
?>

<div id="claudebot-settings" class="section">
    <h2>Claude Bot — Permissions</h2>
    <p class="settings-hint">Manage which users and groups are allowed to interact with the Claude Talk Bot.</p>

    <h3>Allowed Users</h3>
    <div class="claudebot-add-row">
        <div class="claudebot-ac-wrapper">
            <input type="text" id="claudebot-user-input" placeholder="Search user..." autocomplete="off" />
            <div id="claudebot-user-dropdown" class="claudebot-ac-dropdown"></div>
        </div>
        <button id="claudebot-add-user" class="button primary">Add</button>
    </div>
    <table id="claudebot-user-table" class="claudebot-table">
        <thead><tr><th>User</th><th>Added by</th><th>Date</th><th></th></tr></thead>
        <tbody></tbody>
    </table>

    <h3>Allowed Groups</h3>
    <div class="claudebot-add-row">
        <div class="claudebot-ac-wrapper">
            <input type="text" id="claudebot-group-input" placeholder="Search group..." autocomplete="off" />
            <div id="claudebot-group-dropdown" class="claudebot-ac-dropdown"></div>
        </div>
        <button id="claudebot-add-group" class="button primary">Add</button>
    </div>
    <table id="claudebot-group-table" class="claudebot-table">
        <thead><tr><th>Group</th><th>Added by</th><th>Date</th><th></th></tr></thead>
        <tbody></tbody>
    </table>
</div>
