<?php

return [
    'ocs' => [
        ['name' => 'PermissionApi#index', 'url' => '/api/v1/permissions', 'verb' => 'GET'],
        ['name' => 'PermissionApi#create', 'url' => '/api/v1/permissions', 'verb' => 'POST'],
        ['name' => 'PermissionApi#destroy', 'url' => '/api/v1/permissions/{id}', 'verb' => 'DELETE'],
        ['name' => 'PermissionApi#check', 'url' => '/api/v1/check/{userId}', 'verb' => 'GET'],
    ],
];
