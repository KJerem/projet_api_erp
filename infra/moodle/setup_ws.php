<?php
// Active le protocole REST, crée le service externe "opentraining_integration" avec
// les fonctions utilisées par l'Integration Hub et génère (ou réutilise) un token permanent.
// Dernière ligne de sortie = le token (lu par 50-opentraining-setup.sh).

define('CLI_SCRIPT', true);

require('/var/www/html/config.php');
require_once($CFG->libdir . '/clilib.php');
require_once($CFG->dirroot . '/webservice/lib.php');

const SERVICE_SHORTNAME = 'opentraining_integration';
const FUNCTIONS = [
    'core_webservice_get_site_info',
    'core_user_get_users_by_field',
    'core_user_create_users',
    'core_enrol_get_users_courses',
    'enrol_manual_enrol_users',
    'enrol_manual_unenrol_users',
    'core_course_get_courses_by_field',
    'core_course_create_courses',
];

set_config('enablewebservices', 1);

$protocols = array_filter(explode(',', (string) get_config('core', 'webserviceprotocols')));
if (!in_array('rest', $protocols, true)) {
    $protocols[] = 'rest';
    set_config('webserviceprotocols', implode(',', $protocols));
}

$manager = new webservice();
$service = $DB->get_record('external_services', ['shortname' => SERVICE_SHORTNAME]);
if (!$service) {
    $serviceid = $manager->add_external_service((object) [
        'name' => 'OpenTraining Integration',
        'shortname' => SERVICE_SHORTNAME,
        'enabled' => 1,
        'restrictedusers' => 0,
        'downloadfiles' => 0,
        'uploadfiles' => 0,
        'requiredcapability' => '',
    ]);
    $service = $DB->get_record('external_services', ['id' => $serviceid], '*', MUST_EXIST);
}

foreach (FUNCTIONS as $function) {
    if (!$manager->service_function_exists($function, $service->id)) {
        $manager->add_external_function_to_service($function, $service->id);
    }
}

$admin = get_admin();
$token = $DB->get_record('external_tokens', [
    'userid' => $admin->id,
    'externalserviceid' => $service->id,
    'tokentype' => EXTERNAL_TOKEN_PERMANENT,
]);

if ($token) {
    $tokenvalue = $token->token;
} else {
    $tokenvalue = \core_external\util::generate_token(
        EXTERNAL_TOKEN_PERMANENT,
        $service,
        $admin->id,
        context_system::instance(),
        0,
        '',
        'OpenTraining Integration Hub'
    );
}

echo $tokenvalue . PHP_EOL;
