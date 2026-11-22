// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * Read-only toolset for the orchestrator's PLANNER and COLD-READ VERIFIER
 * roles. Only the `read` tool is registered: pi exposes exactly what is
 * registered, so the model has no write, edit, or shell surface at all —
 * enforcement by tool configuration, not by prompt instruction. Reads go to
 * the task container through the taskbridge Relay (TASK_RELAY_URL), same as
 * task-tools.ts. Long outputs get the same truncation note the bash tool
 * uses, pointing the role at the fact rather than hiding it.
 */
import {
	type ExtensionAPI,
	createReadToolDefinition,
} from '@earendil-works/pi-coding-agent';
import http from 'node:http';

const RELAY = process.env.TASK_RELAY_URL ?? "";
const WORKDIR = process.env.TASK_WORKDIR ?? "/";
const IMAGES: Record<string, string> = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
	".webp": "image/webp",
};

interface RelayResponse {
	ok?: boolean;
	error?: string;
	truncated?: boolean;
	size?: number;
	content_b64?: string;
}

function relay(route: string, body: object): Promise<RelayResponse> {
	const url = new URL(`${RELAY}/${route}`);
	const data = Buffer.from(JSON.stringify(body));
	return new Promise((resolve, reject) => {
		const req = http.request(
			{
				hostname: url.hostname,
				port: url.port,
				path: url.pathname,
				method: "POST",
				headers: { "Content-Type": "application/json", "Content-Length": data.length },
			},
			(res) => {
				const chunks: Buffer[] = [];
				res.on("data", (chunk) => chunks.push(chunk));
				res.on("end", () => {
					try {
						const out = JSON.parse(Buffer.concat(chunks).toString() || "{}") as RelayResponse;
						if (res.statusCode !== 200) reject(new Error(out.error ?? `relay answered ${res.statusCode}`));
						else if (out.ok === false) reject(new Error(out.error));
						else resolve(out);
					} catch (err) {
						reject(err);
					}
				});
			},
		);
		req.on("error", reject);
		req.end(data);
	});
}

// tslint:disable-next-line:no-any
export function inTask(tool: any, workdir: string): any {
	return {
		...tool,
		// tslint:disable-next-line:no-any
		execute: (id: string, params: unknown, signal: unknown, onUpdate: unknown, ctx: any) =>
			tool.execute(id, params, signal, onUpdate, ctx && Object.create(ctx, { cwd: { value: workdir } })),
	};
}

// tslint:disable-next-line:no-default-export
export default function registerReadonlyTools(pi: ExtensionAPI) {
	const readOp = async (p: string) => {
		const r = await relay("read_file", { path: p });
		if (r.truncated) throw new Error(`${p} is ${r.size} bytes, over the 10 MB a read returns; read it in parts`);
		return Buffer.from(r.content_b64 ?? "", "base64");
	};
	const access = async () => {};

	pi.registerTool(inTask(
		createReadToolDefinition(WORKDIR, {
			operations: {
				readFile: readOp,
				access,
				detectImageMimeType: async (p) => IMAGES[path_ext(p)] ?? null,
			},
		}),
		WORKDIR,
	));

	pi.on("before_agent_start", (event) => {
		event.systemPromptOptions.cwd = WORKDIR;
	});
}

function path_ext(p: string): string {
	const i = p.lastIndexOf(".");
	return i >= 0 ? p.slice(i).toLowerCase() : "";
}
