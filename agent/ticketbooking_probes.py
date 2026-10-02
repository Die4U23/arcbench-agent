"""Local journey checks derived from observed defects; not official case bodies."""

JOURNEY_PROBES = r"""
test('REQ-3.2: local probe - one selected-train summary during review', async ({page}) => {
  const account=uniqueTicketBookingAccount();
  const criteria={from:'Shanghai',to:'Beijing',date:'Sun, May 31'};
  const passenger={name:'Nguyen Duc Minh',idNumber:'C612345677',nationality:'Vietnam'};
  await registerAccount(page,account); await expectSignedIn(page,account.username);
  await openBookingPage(page,criteria,'G532');
  await fillBookingForm(page,'standing ticket',passenger);
  await page.getByRole('button',{name:/place order/i}).click();
  await expect(page.getByText(/please confirm the following information/i)).toBeVisible();
  await expect(page.getByText('G532',{exact:false})).toHaveCount(1);
});
test('REQ-3.1: local probe - an older booking cannot replace a newly selected journey', async ({page}) => {
  const account=uniqueTicketBookingAccount();
  const criteria={from:'Shanghai',to:'Beijing',date:'Sun, May 31'};
  const passenger={name:'Nguyen Duc Minh',idNumber:'C612345677',nationality:'Vietnam'};
  await registerAccount(page,account); await expectSignedIn(page,account.username);
  await openBookingPage(page,criteria,'G532');
  await fillBookingForm(page,'standing ticket',passenger);
  await page.getByRole('button',{name:/place order/i}).click();
  await page.getByRole('button',{name:/^confirm$/i}).click();
  await expect(page.getByText(/booking number/i)).toBeVisible();
  await searchTrains(page,{from:'Beijing',to:'Tianjin',date:'Sun, May 31'});
  await openBookableTrain(page,'G561');
  await expect(page.getByRole('heading',{name:/passenger information/i})).toBeVisible();
  await expect(page.getByText('G561',{exact:false})).toBeVisible();
  await expect(page.getByText('G532',{exact:false})).toHaveCount(0);
  await expect(page.getByText(/booking number/i)).toHaveCount(0);
});
"""

JOURNEY_PROBE_COUNT = 2
