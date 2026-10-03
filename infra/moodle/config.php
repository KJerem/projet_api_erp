<?php  // Moodle configuration file — valeurs injectées par variables d'environnement (aucun secret en dur).

unset($CFG);
global $CFG;
$CFG = new stdClass();

$CFG->dbtype    = 'pgsql';
$CFG->dblibrary = 'native';
$CFG->dbhost    = getenv('MOODLE_DB_HOST') ?: 'moodle-db';
$CFG->dbname    = getenv('MOODLE_DB_NAME') ?: 'moodle';
$CFG->dbuser    = getenv('MOODLE_DB_USER') ?: 'moodle';
$CFG->dbpass    = getenv('MOODLE_DB_PASSWORD');
$CFG->prefix    = 'mdl_';
$CFG->dboptions = ['dbpersist' => 0, 'dbport' => 5432];

// Moodle est appelé à la fois depuis le navigateur (localhost:8081) et depuis le réseau
// Docker (http://moodle) : le wwwroot suit l'en-tête Host, avec un repli pour la CLI.
$host = $_SERVER['HTTP_HOST'] ?? null;
$CFG->wwwroot = $host ? 'http://' . $host : (getenv('MOODLE_WWWROOT') ?: 'http://localhost:8081');

$CFG->dataroot  = '/var/www/moodledata';
$CFG->admin     = 'admin';
$CFG->directorypermissions = 02777;
$CFG->routerconfigured = false;

// Environnement de démo : Moodle n'envoie aucun email (les notifications passent par Mailpit via le Hub).
$CFG->noemailever = true;

require_once(__DIR__ . '/lib/setup.php'); // Do not edit
