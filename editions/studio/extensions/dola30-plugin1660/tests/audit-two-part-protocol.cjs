// Read-only offline audit. No browser/network access and no output file writes.
// Usage: node audit-two-part-protocol.cjs <debugger.json> [...]
const fs = require('node:fs');
const path = require('node:path');
const candidate = /(?:^|[._])(?:count|num|quantity|batch|split|segment|sample|output)(?:[._]|$)|server_do_compare/i;
function audit(report) {
  return (report.debuggerNetwork?.requests || []).filter(record => record.kind === 'submission').map(record => {
    const detail = record.request?.comparison?.fieldDetails;
    return { id: record.id, httpStatus: record.status, phase: record.phase,
      video: record.request?.video || null,
      inputBoxKeys: record.request?.inputBoxKeys || [],
      fieldCoverageComplete: detail?.complete === true,
      inspectedFieldCount: detail?.fields?.length || 0,
      candidates: (detail?.fields || []).filter(field => candidate.test(field.path)).map(field => ({
        path: field.path, type: field.type,
        // Do not export unknown strings, identifiers, tokens, or hashes.
        ...(['boolean', 'number'].includes(typeof field.value) ? { value: field.value } : {})
      })),
      structuredTaskCandidates: record.response?.taskFields?.length || 0,
      structuredBillingCandidates: record.response?.billingFields?.length || 0,
      verdict: 'UNVERIFIED: field presence/absence and HTTP 200 do not establish two-part generation support' };
  });
}
if (require.main === module) {
  const files = process.argv.slice(2);
  if (!files.length) { console.error('Pass one or more exported debugger JSON files.'); process.exitCode = 2; }
  else {
    const reports = files.map(file => ({ file: path.basename(file), submissions: audit(JSON.parse(fs.readFileSync(file, 'utf8'))) }));
    const configs = ['dola', 'doubao'].map(site => {
      const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', `${site}-skill-pack-response.json`), 'utf8'));
      return { site, source: 'bundled snapshot, not live server capability',
        videoOptionKeys: (config.data?.video_generation?.meta?.option_list || []).map(option => option.value) };
    });
    console.log(JSON.stringify({ reports, configs }, null, 2));
  }
}
module.exports = { audit };
