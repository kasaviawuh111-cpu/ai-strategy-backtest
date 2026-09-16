// Scoped release helper. Credentials remain in memory; never log raw service config.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const cli = '/Users/mima0000/.npm/_npx/f4a3769003ccd80b/node_modules/@cloudbase/cli/bin/tcb';
const envId = 'test-d6gwxiamcd87743af';
const service = 'ashare-strategy-preview';
const bundle = '/private/tmp/ashare-public-release-20260916-guide';
const output = path.join(root, 'outputs/public-release-20260916-guide');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function call(serviceName, action, body, version) {
  // Pass request over stdin, not OS argv (EnvParams contains existing credentials).
  const runner = `const fs=require('node:fs');const a=JSON.parse(fs.readFileSync(0,'utf8'));process.argv=['node',${JSON.stringify(cli)},'api',a.service,a.action,'-e',${JSON.stringify(envId)},'--body',JSON.stringify(a.body),'--api-version',a.version,'--json'];require(${JSON.stringify(cli)});`;
  const raw = execFileSync(process.execPath, ['-e', runner], {
    input: JSON.stringify({ service: serviceName, action, body, version }), encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'], maxBuffer: 8 * 1024 * 1024,
  });
  const parsed = JSON.parse(raw.slice(raw.indexOf('{')));
  const data = parsed.data || parsed;
  if (data.Error) throw new Error(`${action}: ${data.Error.Code}`);
  return data;
}
function save(name, value) {
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, name), JSON.stringify(value, null, 2) + '\n');
}
async function main() {
  const manifest = JSON.parse(fs.readFileSync(path.join(bundle, 'source-manifest.json')));
  for (const record of manifest.files) {
    const source = manifest.sourcePathMappings[record.path] || record.path;
    for (const file of [path.join(root, source), path.join(bundle, record.path)]) {
      if (sha(fs.readFileSync(file)) !== record.sha256) throw new Error('Source changed: ' + record.path);
    }
  }
  const detail = call('tcbr', 'DescribeCloudRunServerDetail', { EnvId: envId, ServerName: service }, '2022-02-17');
  const config = detail.ServerConfig;
  const env = JSON.parse(config.EnvParams);
  if (env.RESEARCH_PROVIDER_MODE !== 'tencent_web_search' || env.MINUTE_GRID_ENABLED !== 'true') throw new Error('Required capability configuration differs');
  if (!config.VolumesConf.some(v => v.BucketName === 'ashare-minute-data-1330091763' && v.DstPath === '/data' && v.ReadOnly)) throw new Error('Missing private minute mount');
  const keys = ['CODE_REVISION', 'RESEARCH_PROVIDER_MODE', 'MINUTE_GRID_ENABLED', 'EXTERNAL_MINUTE_ROOT', 'MINUTE_MARKET_CALENDAR_PATH', 'CANDIDATE_PROVIDER_MODEL', 'PLAN_DEEP_PROVIDER_MODEL', 'PLAN_DEEP_PROVIDER_THINKING', 'PLAN_DEEP_PROVIDER_REASONING_EFFORT'];
  const audit = { checkedAt: new Date().toISOString(), candidateRevision: manifest.codeRevision,
    fileCount: manifest.files.length, onlineVersions: detail.OnlineVersionInfos,
    configuration: Object.fromEntries(keys.map(k => [k, env[k]])),
    credentialPresence: Object.fromEntries(Object.keys(env).filter(k => /KEY|TOKEN|SECRET/.test(k)).map(k => [k, Boolean(env[k])])),
    mounts: config.VolumesConf.map(({ Type, BucketName, DstPath, SrcPath, ReadOnly }) => ({ Type, BucketName, DstPath, SrcPath, ReadOnly })),
    resources: { cpu: config.Cpu, memory: config.Mem, min: config.MinNum, max: config.MaxNum },
  };
  if (process.argv[2] !== 'deploy') { save('preflight.json', audit); console.log(JSON.stringify(audit)); return; }
  if (fs.existsSync(path.join(output, 'deployment.json'))) throw new Error('Deployment already submitted; inspect existing job instead of repeating');
  save('preflight.json', audit);
  const build = call('tcb', 'DescribeCloudBaseBuildService', { EnvId: envId, ServiceName: service }, '2018-06-08');
  if (!build.UploadUrl || !build.PackageName || !build.PackageVersion) throw new Error('Upload contract incomplete');
  const archive = path.join(output, 'source.zip');
  if (fs.existsSync(archive)) throw new Error('Archive already exists; inspect previous attempt');
  execFileSync('zip', ['-qr', archive, '.'], { cwd: bundle });
  const response = await fetch(build.UploadUrl, { method: 'PUT',
    headers: Object.fromEntries((build.UploadHeaders || []).map(h => [h.Key, h.Value])),
    body: fs.readFileSync(archive) });
  if (!response.ok) throw new Error('Upload failed: ' + response.status);
  save('upload.json', { packageName: build.PackageName, packageVersion: build.PackageVersion, archiveSha256: sha(fs.readFileSync(archive)), revision: manifest.codeRevision });
  env.CODE_REVISION = manifest.codeRevision;
  // A single diff item preserves ALL other service settings, including COS mount.
  const result = call('tcbr', 'UpdateCloudRunServer', {
    EnvId: envId, ServerName: service,
    DeployInfo: { DeployType: 'package', PackageName: build.PackageName, PackageVersion: build.PackageVersion, ReleaseType: 'GRAY' },
    Items: [{ Key: 'EnvParam', Value: JSON.stringify(env) }],
  }, '2022-02-17');
  save('deployment.json', { submittedAt: new Date().toISOString(), revision: manifest.codeRevision, result });
  console.log(JSON.stringify({ revision: manifest.codeRevision, result }));
}
main().catch(e => { console.error(e instanceof Error && !('stdout' in e) ? e.message : 'Cloud API operation failed; raw output suppressed to protect credentials'); process.exitCode = 1; });
