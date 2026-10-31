// Versioned entry: command-line loaded profiles can retain an older worker at
// service-worker.js even when the unpacked manifest/content scripts are newer.
// A new entry URL registers a fresh worker without deleting any profile data.
importScripts("service-worker.js");
