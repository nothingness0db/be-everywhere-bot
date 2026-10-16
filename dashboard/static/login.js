const form = document.querySelector('#login-form');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button');
  const error = document.querySelector('#login-error');
  error.hidden = true; button.disabled = true; button.textContent = '正在登录…';
  try {
    const response = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:document.querySelector('#password').value})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '登录失败，请重试。');
    location.replace('/');
  } catch (e) {error.textContent = e.message || '连接失败，请检查服务是否运行。'; error.hidden = false;}
  finally {button.disabled = false; button.textContent = '进入工作台 →';}
});
