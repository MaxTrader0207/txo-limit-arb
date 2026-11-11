// 準時觸發 GitHub Actions：Cloudflare Cron Triggers 呼叫 workflow_dispatch。
// GitHub 自帶排程在整點、半點常延遲或被丟棄，所以由這裡當主要觸發來源。
// 需要的設定（見 README）：
//   wrangler secret put GITHUB_TOKEN   ← fine-grained PAT，只授權本 repo 的 Actions: Read and write
const LIVE_CRONS = new Set(["37 1 * * MON-FRI", "7 3 * * MON-FRI", "17 4 * * MON-FRI"]);

async function dispatch(env, mode) {
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/scan.yml/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "txo-limit-arb-cron",
    },
    body: JSON.stringify({ ref: "main", inputs: { mode } }),
  });
  if (r.status !== 204) throw new Error(`dispatch ${mode} 失敗：${r.status} ${await r.text()}`);
  console.log(`dispatched ${mode}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, LIVE_CRONS.has(event.cron) ? "live" : "run"));
  },
  async fetch() {
    return new Response("txo-limit-arb cron trigger", { status: 200 });
  },
};
