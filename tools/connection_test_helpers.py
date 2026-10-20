"""UI navigation helpers for the unified connection modal; no direct state mutation."""
async def open_connection(page):
    if not await page.locator('#connection-dialog').evaluate('e=>e.open'):
        await page.locator('#view-menu summary').click()
        await page.locator('[data-connection-entry]').click()
    await page.wait_for_function("document.getElementById('connection-dialog').open && !connectionLoading")

async def advanced(page):
    await open_connection(page)
    if not await page.locator('#connection-advanced').evaluate('e=>e.open'):
        await page.locator('#connection-advanced > summary').click()

async def open_catalog(page):
    await advanced(page)
    await page.locator('#btn-drafts').click()

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
    await advanced(page)
    await page.locator('#connection-advanced [data-ipv6-entry]').click()
