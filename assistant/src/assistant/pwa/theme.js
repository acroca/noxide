// Classic script in <head>, after the stylesheet: it runs before first paint,
// so an explicit choice never flashes the device scheme. Without a stored
// choice the attribute stays off and style.css follows prefers-color-scheme.
(() => {
  const KEY = 'noxide-theme';
  const meta = document.querySelector('meta[name="theme-color"]');
  const stored = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const apply = preference => {
    const root = document.documentElement;
    if (preference === 'light' || preference === 'dark') root.dataset.theme = preference;
    else delete root.dataset.theme;
    // The status bar colour on installed apps follows the sidebar surface.
    const side = getComputedStyle(root).getPropertyValue('--side').trim();
    if (meta && side) meta.content = side;
  };
  apply(stored());
  window.applyTheme = apply;
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => apply(stored()));
})();
