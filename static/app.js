'use strict';

document.addEventListener('click', async (event) => {
  const id = event.target && event.target.dataset && event.target.dataset.copy;
  if (!id) return;
  const source = document.getElementById(id);
  if (!source) return;
  await navigator.clipboard.writeText(source.textContent);
  const old = event.target.textContent;
  event.target.textContent = 'Copied';
  window.setTimeout(() => { event.target.textContent = old; }, 1200);
});

document.querySelectorAll('input[type=file][data-fill]').forEach((input) => {
  input.addEventListener('change', () => {
    const file = input.files && input.files[0];
    const target = document.getElementById(input.dataset.fill);
    if (!file || !target) return;
    if (file.size > 256 * 1024) {
      input.value = '';
      window.alert('That file exceeds 256 KB.');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => { target.value = String(reader.result || ''); };
    reader.readAsText(file);
  });
});
