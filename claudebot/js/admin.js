(function() {
    'use strict';

    var baseUrl = OC.getRootPath() + '/ocs/v2.php/apps/claudebot/api/v1';
    var autocompleteUrl = OC.getRootPath() + '/ocs/v2.php/core/autocomplete/get';

    function apiCall(method, path, data) {
        var url = baseUrl + path;
        url += (url.indexOf('?') === -1 ? '?' : '&') + 'format=json';
        return $.ajax({
            url: url,
            method: method,
            contentType: 'application/json',
            dataType: 'json',
            data: data ? JSON.stringify(data) : undefined,
            headers: {
                'OCS-APIRequest': 'true',
                'Accept': 'application/json'
            }
        });
    }

    function formatDate(timestamp) {
        if (!timestamp) return '-';
        var d = new Date(timestamp * 1000);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
    }

    // --- Autocomplete ---

    function setupAutocomplete(inputId, dropdownId, shareType) {
        var $input = $(inputId);
        var $dropdown = $(dropdownId);
        var debounceTimer = null;
        var selectedValue = null;

        $input.on('input', function() {
            selectedValue = null;
            var query = $input.val().trim();
            if (query.length < 1) {
                $dropdown.hide().empty();
                return;
            }
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function() {
                searchNC(query, shareType, $dropdown, $input);
            }, 250);
        });

        $input.on('focus', function() {
            var query = $input.val().trim();
            if (query.length >= 1) {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(function() {
                    searchNC(query, shareType, $dropdown, $input);
                }, 100);
            }
        });

        // hide dropdown on outside click
        $(document).on('mousedown', function(e) {
            if (!$(e.target).closest(inputId + ', ' + dropdownId).length) {
                $dropdown.hide();
            }
        });

        // return getter for selected value
        return function() {
            return selectedValue || $input.val().trim();
        };
    }

    function searchNC(query, shareType, $dropdown, $input) {
        var url = autocompleteUrl + '?search=' + encodeURIComponent(query)
            + '&itemType=&itemId=&shareTypes%5B%5D=' + shareType
            + '&limit=10&format=json';

        $.ajax({
            url: url,
            method: 'GET',
            headers: { 'OCS-APIRequest': 'true', 'Accept': 'application/json' },
            dataType: 'json'
        }).done(function(response) {
            var results = response.ocs ? response.ocs.data : [];
            $dropdown.empty();
            if (results.length === 0) {
                $dropdown.append('<div class="claudebot-ac-item claudebot-ac-empty">No results</div>');
            } else {
                results.forEach(function(item) {
                    var $item = $('<div class="claudebot-ac-item">');
                    var icon = shareType === 0 ? '👤' : '👥';
                    var label = item.label || item.id;
                    var sub = item.subline || item.shareWithDisplayNameUnique || '';
                    var html = '<span class="claudebot-ac-icon">' + icon + '</span>';
                    html += '<span class="claudebot-ac-label">' + escapeHtml(label) + '</span>';
                    if (sub) {
                        html += '<span class="claudebot-ac-sub">' + escapeHtml(sub) + '</span>';
                    }
                    $item.html(html);
                    $item.on('mousedown', function(e) {
                        e.preventDefault();
                        $input.val(item.id);
                        $input.data('selectedId', item.id);
                        $dropdown.hide();
                    });
                    $dropdown.append($item);
                });
            }
            $dropdown.show();
        }).fail(function() {
            $dropdown.hide();
        });
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // --- Permissions table ---

    function renderTable(tableId, permissions, type) {
        var $tbody = $(tableId + ' tbody');
        $tbody.empty();

        var items = permissions.filter(function(p) { return p.type === type; });

        if (items.length === 0) {
            $tbody.append('<tr><td colspan="4" class="claudebot-empty">No entries</td></tr>');
            return;
        }

        items.forEach(function(p) {
            var $row = $('<tr>');
            var icon = type === 'user' ? '👤 ' : '👥 ';
            $row.append($('<td>').text(icon + p.target));
            $row.append($('<td>').text(p.addedBy || '-'));
            $row.append($('<td>').text(formatDate(p.addedAt)));
            var $btn = $('<button class="claudebot-delete">').text('Remove');
            $btn.on('click', function() { deletePermission(p.id); });
            $row.append($('<td>').append($btn));
            $tbody.append($row);
        });
    }

    function loadPermissions() {
        apiCall('GET', '/permissions').done(function(response) {
            var data = response.ocs ? response.ocs.data : response;
            if (!Array.isArray(data)) {
                console.error('[ClaudeBot] Unexpected API response:', response);
                renderTable('#claudebot-user-table', [], 'user');
                renderTable('#claudebot-group-table', [], 'group');
                return;
            }
            renderTable('#claudebot-user-table', data, 'user');
            renderTable('#claudebot-group-table', data, 'group');
        }).fail(function(xhr) {
            console.error('[ClaudeBot] Failed to load permissions:', xhr.status, xhr.responseText);
            OC.Notification.showTemporary('Failed to load permissions (HTTP ' + xhr.status + ')');
        });
    }

    function addPermission(type, target) {
        if (!target || !target.trim()) {
            OC.Notification.showTemporary('Please enter a name');
            return;
        }
        apiCall('POST', '/permissions', { type: type, target: target.trim() })
            .done(function() {
                loadPermissions();
                OC.Notification.showTemporary(type === 'user' ? 'User added' : 'Group added');
            })
            .fail(function(xhr) {
                var msg = 'Error';
                try { msg = xhr.responseJSON.ocs.data.message || msg; } catch(e) {}
                OC.Notification.showTemporary(msg);
            });
    }

    function deletePermission(id) {
        OC.dialogs.confirmDestructive(
            'Really remove this permission?',
            'Remove permission',
            {
                type: OC.dialogs.YES_NO_BUTTONS,
                confirm: 'Remove',
                confirmClasses: 'error',
                cancel: 'Cancel'
            },
            function(confirmed) {
                if (!confirmed) return;
                apiCall('DELETE', '/permissions/' + id)
                    .done(function() {
                        loadPermissions();
                        OC.Notification.showTemporary('Permission removed');
                    })
                    .fail(function() {
                        OC.Notification.showTemporary('Failed to remove permission');
                    });
            },
            true
        );
    }

    // --- Init ---

    $(document).ready(function() {
        var getUserValue = setupAutocomplete('#claudebot-user-input', '#claudebot-user-dropdown', 0);
        var getGroupValue = setupAutocomplete('#claudebot-group-input', '#claudebot-group-dropdown', 1);

        loadPermissions();

        $('#claudebot-add-user').on('click', function() {
            var val = getUserValue();
            addPermission('user', val);
            $('#claudebot-user-input').val('');
            $('#claudebot-user-dropdown').hide();
        });

        $('#claudebot-add-group').on('click', function() {
            var val = getGroupValue();
            addPermission('group', val);
            $('#claudebot-group-input').val('');
            $('#claudebot-group-dropdown').hide();
        });

        $('#claudebot-user-input').on('keypress', function(e) {
            if (e.which === 13) {
                e.preventDefault();
                $('#claudebot-add-user').click();
            }
        });

        $('#claudebot-group-input').on('keypress', function(e) {
            if (e.which === 13) {
                e.preventDefault();
                $('#claudebot-add-group').click();
            }
        });
    });
})();
