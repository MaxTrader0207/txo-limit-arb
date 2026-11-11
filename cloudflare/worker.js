// txo-limit-arb 準時觸發器：Cloudflare Cron Triggers → GitHub workflow_dispatch
// GitHub 自帶排程在整點、半點常延遲或被丟棄，所以由這裡當主要觸發來源。
//
// 需要的設定（Cloudflare Worker → Settings → Variables and Secrets）：
//   REPO          文字：MaxTrader0207/txo-limit-arb
//   GITHUB_TOKEN  密鑰：fine-grained token，只授權 txo-limit-arb 的 Actions: Read and write
//
// 模式依觸發時間（UTC）判斷：01、03、04 點＝結算日盤中快照（live），其他＝收盤掃描（run）。
// 打開這個 Worker 的網址會檢查 token 是否可用（不會觸發任何執行）。

const LIVE_HOURS_UTC = new Set([1, 3, 4]);

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "txo-limit-arb-cron",
      ...(init.headers || {}),
    },
  });
}

async function dispatch(env, mode) {
  const r = await gh(env, "/actions/workflows/scan.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { mode } }),
  });
  if (r.status !== 204) throw new Error(`觸發 ${mode} 失敗：${r.status} ${await r.text()}`);
  console.log(`已觸發 ${mode}`);
}

export default {
  async scheduled(controller, env, ctx) {
    const hour = new Date(controller.scheduledTime).getUTCHours();
    ctx.waitUntil(dispatch(env, LIVE_HOURS_UTC.has(hour) ? "live" : "run"));
  },

  async fetch(request, env) {
    const html = (msg, ok) => new Response(
      `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">` +
      `<body style="font:16px/1.6 sans-serif;padding:24px;background:${ok ? "#e8f5e9" : "#ffebee"}">${msg}</body>`,
      { headers: { "content-type": "text/html; charset=utf-8" } });
    if (!env.REPO) return html("❌ 尚未設定變數 REPO", false);
    if (!env.GITHUB_TOKEN) return html("❌ 尚未設定密鑰 GITHUB_TOKEN", false);
    const r = await gh(env, "/actions/workflows/scan.yml");
    if (r.status === 200) {
      const w = await r.json();
      return html(`✅ token 可用，已連到 ${env.REPO} 的「${w.name}」（${w.state}）。<br>` +
                  `觸發權限會在下一次排程時確認，可到 GitHub Actions 看是否出現由你帳號觸發的執行。`, true);
    }
    return html(`❌ token 無法讀取 ${env.REPO}：HTTP ${r.status}。<br>請確認 token 有選到這個 repo，且 Actions 權限為 Read and write。`, false);
  },
};
