/* Prints the dashboard's command catalog (dashboard/src/lib/agents.ts) as JSON,
   by running the TypeScript itself, so pytest compares what the dashboard
   really shows with remembra.dev/setup.md and remembra_setup.

   Needs Node's type stripping (node --experimental-strip-types, Node 22.6+).
   agents.ts imports its neighbours without an extension, as the bundler
   allows; the resolve hook below adds ".ts" for those.

   usage: node --experimental-strip-types tests/js/agents_catalog.mjs <agents.ts> <server url>
   stdout: {"PIPX_INSTALL": ..., "saveKeyCommand": {"": ..., "<url>": ...}, ...} */
import { register } from "node:module";
import { pathToFileURL } from "node:url";

const hooks = `
export async function resolve(specifier, context, next) {
  if (/^\\.{1,2}\\//.test(specifier) && !/\\.[cm]?[jt]sx?$/.test(specifier)) {
    return next(specifier + ".ts", context);
  }
  return next(specifier, context);
}`;
register(`data:text/javascript,${encodeURIComponent(hooks)}`, import.meta.url);

const [file, server] = process.argv.slice(2);
const a = await import(pathToFileURL(file).href);
const each = (fn, args) => Object.fromEntries(args.map((arg) => [arg ?? "", fn(arg)]));

const agents = a.CONNECTABLE_AGENTS;
process.stdout.write(
  JSON.stringify({
    PIPX_INSTALL: a.PIPX_INSTALL,
    CONNECTABLE_AGENTS: agents,
    DOCTOR_RELEASE: a.DOCTOR_RELEASE,
    saveKeyCommand: each(a.saveKeyCommand, ["", server]),
    oneLineInstall: each(a.oneLineInstall, ["", server]),
    agentConnectCommand: each(a.agentConnectCommand, agents),
    doctorCommand: each(a.doctorCommand, [null, ...agents]),
    pipxRunDoctorCommand: each(a.pipxRunDoctorCommand, [null, ...agents]),
    askAgentDoctor: each(a.askAgentDoctor, [null, ...agents]),
    UNINSTALL_STEPS: a.UNINSTALL_STEPS,
    names: Object.fromEntries(agents.map((id) => [id, a.agentMeta(id).name])),
    verified: Object.fromEntries(agents.map((id) => [id, Boolean(a.agentMeta(id).verified)])),
  }),
);
