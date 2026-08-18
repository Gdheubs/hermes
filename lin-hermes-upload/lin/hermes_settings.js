(async function () {
  const state = document.getElementById('runtime-state');
  try {
    const response = await fetch('/hermes/runtime-status');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Runtime unavailable');
    state.textContent = data.configured
      ? 'Hermes Runtime 已連線，可由 Lin 管理 Agent 能力。'
      : 'Hermes Runtime 尚未設定。加入 Render 服務 URL 與服務 Token 後啟用。';
    state.classList.add(data.configured ? 'ok' : 'error');
  } catch (error) {
    state.textContent = '無法讀取 Hermes Runtime 狀態。';
    state.classList.add('error');
  }
})();
