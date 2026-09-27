"""在真实 Chromium 里跑导出弹窗逻辑，DOM 仿 09-27 WoS 截图。

复现的两个坑：DOM 前部残留隐藏的 role=dialog（裸 .first 会拿到它）；
弹窗两个范围输入框没有 From/To 标签。背景工具栏另有一个 Export 按钮，绝不能被点。
"""

import unittest
from pathlib import Path

from wos_zotero_kit import human_input, wos_browser

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

WOS_EXPORT_DOM = """
<html><body>
<div role="dialog" style="display:none"><button>Export</button><button>Cancel</button></div>
<div class="toolbar"><button id="bg-export" onclick="window.clicked.push('background')">Export</button></div>
<div class="overlay">
  <div class="modal">
    <h2>Export Records to RIS File</h2>
    <div>Record Options</div>
    <label><input type="radio" name="opt" checked> All records on page</label>
    <label><input type="radio" name="opt" id="range"> Records from:</label>
    <input type="text" id="from" disabled> to <input type="text" id="to" disabled>
    <div>Record Content:</div>
    <div class="dropdown" id="content" onclick="document.getElementById('menu').style.display='block'">Author, Title, Source</div>
    <ul id="menu" style="display:none">
      <li role="option" onclick="pick(this)">Author, Title, Source</li>
      <li role="option" onclick="pick(this)">Full Record</li>
      <li role="option" onclick="pick(this)">Full Record and Cited References</li>
    </ul>
    <button id="confirm" onclick="window.clicked.push('dialog')">Export</button>
    <button>Cancel</button>
  </div>
</div>
<script>
window.clicked = [];
document.getElementById('range').addEventListener('change', () => {
  document.getElementById('from').disabled = false;
  document.getElementById('to').disabled = false;
});
function pick(el) {
  document.getElementById('content').textContent = el.textContent;
  document.getElementById('menu').style.display = 'none';
}
</script>
</body></html>
"""


@unittest.skipIf(async_playwright is None, "playwright not installed")
class WosExportDialogDomTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:  # 本机没装 Playwright Chromium 时跳过，不算失败
            await self._pw.stop()
            self.skipTest(f"chromium unavailable: {exc}")
        self.page = await self._browser.new_page()
        await self.page.set_content(WOS_EXPORT_DOM)
        # 真人化输入的延迟在这里没有意义，换成直接点击/键入
        self._orig = (human_input.human_click, human_input.human_type)
        human_input.human_click = lambda page, loc, rng: loc.click(timeout=2_000)
        human_input.human_type = lambda page, loc, text, rng: loc.fill(text, timeout=2_000)

    async def asyncTearDown(self):
        human_input.human_click, human_input.human_type = self._orig
        await self._browser.close()
        await self._pw.stop()

    async def test_dialog_found_despite_hidden_decoy(self):
        dialog = await wos_browser._wait_for_export_dialog(self.page, timeout_ms=3_000)
        self.assertIsNotNone(dialog)
        self.assertIn("Record Options", await dialog.inner_text())

    async def test_full_record_range_and_confirm_stay_inside_dialog(self):
        dialog = await wos_browser._wait_for_export_dialog(self.page, timeout_ms=3_000)
        self.assertTrue(await wos_browser._choose_full_record_if_present(dialog))
        await wos_browser._set_export_limit_if_present(dialog, 125)
        self.assertTrue(await wos_browser._click_dialog_export_button(dialog, timeout_ms=3_000))

        self.assertEqual(await self.page.inner_text("#content"), "Full Record")
        self.assertTrue(await self.page.is_checked("#range"))
        self.assertEqual(await self.page.input_value("#from"), "1")
        self.assertEqual(await self.page.input_value("#to"), "125")
        self.assertEqual(await self.page.evaluate("window.clicked"), ["dialog"])


if __name__ == "__main__":
    unittest.main()
