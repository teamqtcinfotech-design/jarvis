<?php
/*
 * Copy to config.local.php (same folder) and fill in. config.local.php is blocked from the web
 * by this folder's .htaccess. Never commit it. Never reuse the Laravel APP_KEY or any AI key here.
 */
return [
    'enabled'        => true,
    'writes_enabled' => false,            // keep false until read-only checks pass, then true for the pilot
    'environment'    => 'production',     // must equal TASKPANEL_ENV on the JARVIS server
    'instance_id'    => 'taskpanel-production-01', // must equal TASKPANEL_EXPECTED_INSTANCE on JARVIS

    // One entry per JARVIS server/environment. Secret = long random string shared ONLY with that server.
    'clients' => [
        'django-production-01' => [
            'secret'      => 'REPLACE-WITH-64-RANDOM-CHARACTERS',
            'environment' => 'production',
            'scopes'      => ['directory.read', 'tasks.read', 'tasks.create'],
        ],
    ],

    'timestamp_tolerance_seconds' => 300,
    'max_body_bytes'              => 65536,
    'feed_retention_days'         => 30,
    'feed_page_default'           => 100,
    'feed_page_max'               => 500,
    'detect_min_interval_seconds' => 3,    // change detection runs at most this often from GET /changes

    // ---- TaskPanel schema mapping (READS only). VERIFY against the real database with bin/self-check.php.
    'schema' => [
        'task_table'        => 'taskpanel_tasks',
        'task_id'           => 'id',
        'task_title'        => 'title',
        'task_status'       => 'status',        // todo | working | waiting | done
        'task_priority'     => 'priority',      // normal | high | urgent
        'task_assignee'     => 'assignee_key',  // TaskPanel user_key of the assignee
        'task_sender'       => 'sender_key',    // TaskPanel user_key of the sender
        'task_category'     => 'category',
        'task_due'          => 'due_at',
        'task_version'      => 'version',
        'task_completed_at' => 'completed_at',
        'task_updated_at'   => 'updated_at',
        'task_hold_reason'  => 'hold_reason',   // '' if TaskPanel stores it elsewhere
        'activity_table'    => 'taskpanel_activity',
        'activity_task_id'  => 'task_id',
        'activity_created'  => 'created_at',
    ],
];
