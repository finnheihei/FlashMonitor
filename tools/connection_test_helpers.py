"""UI navigation helpers for connection and maintenance entries; no state mutation."""
async def open_settings(page, pane):
    if await page.locator('#connection-dialog').evaluate('e=>e.open'):
        await page.locator('#connection-close').click()
    if not await page.locator('#set-form .set-nav').count():
        await page.locator('#btn-settings').click()
    await page.locator(f'#set-form .set-nav [data-pane="{pane}"]').click()

async def open_connection(page):
    if not await page.locator('#connection-dialog').evaluate('e=>e.open'):
        await open_settings(page, 'connect')
        await page.locator('[data-connection-entry]').click()
    await page.wait_for_function("document.getElementById('connection-dialog').open && !connectionLoading")

async def advanced(page):
    # Retained for historical GPT/rule-editor scripts targeting the old modal.
    await open_connection(page)
    if not await page.locator('#connection-advanced').evaluate('e=>e.open'):
        await page.locator('#connection-advanced > summary').click()

async def open_catalog(page):
    await open_maintenance(page)
    await page.locator('#settings-advanced [data-open-drafts]').click()

async def open_maintenance(page):
    await open_settings(page, 'data')
    if not await page.locator('#settings-advanced').evaluate('e=>e.open'):
        await page.locator('#settings-advanced > summary').click()

async def open_rule(page,process):
    if not await page.locator('#connection-dialog').evaluate('e=>e.open'):
        await page.locator('.program-detail [data-program-menu="'+process+'"]').click()
        await page.locator('#program-menu [data-menu-action=connection]').click()
        await page.wait_for_function("!document.getElementById('connection-confirm').disabled && document.getElementById('gpt-dialog').getClientRects().length>0")
    await advanced(page)
    await page.locator('#connection-rule').click()

async def open_gpt(page):
    await open_connection(page)
    await page.wait_for_function('gptCatalog && !gptBusy')
    await advanced(page)
    if not await page.locator('#connection-diagnostics').evaluate('e=>e.open'):
        await page.locator('#connection-diagnostics > summary').click()

async def open_legacy(page):
    await open_maintenance(page)
    await page.locator('#settings-advanced [data-ipv6-entry]').click()
