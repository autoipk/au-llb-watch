# AU LLB notification watcher (cloud, runs 24/7 on GitHub)

This checks the Andhra University admissions page and the APLAWCET portal every 10 minutes on GitHub's servers. Your laptop can be off. Setup takes about 10 minutes, once, and is easiest on the laptop.

**Law notices** (LL.B / LL.M / LAWCET) send a loud phone push and an email. **Spot or vacancy notices** for any course send a push and an email. **Every other new notice** sends a quiet push and an email. You'll also get alerts for law PDFs silently replaced, notice wording changes, and the site being down for 3+ hours. A "still working" message arrives at 9 AM IST every day.

## Step 0: Remove the laptop version
Open the **AU Watcher** folder on your Desktop, double-click **Uninstall.bat**, and answer `y`. You can keep the same ntfy topic on your phone.

## Step 1: Create the repository
1. Sign in at https://github.com (create a free account if needed).
2. Top-right **+**, then **New repository**.
3. Name: `au-llb-watch`. Choose **Public** (public repos get unlimited free run time; your email and topic stay hidden in Secrets). Tick **Add a README file**, then **Create repository**.

## Step 2: Upload the files
1. Click **Add file**, then **Upload files**.
2. Open the unzipped `au-llb-cloud` folder in File Explorer. Select everything inside it, **including the `.github` folder**, and drag it onto the page. Then click **Commit changes**.
3. Check that the repository now shows `.github/workflows/au-watch.yml`. If the `.github` folder didn't upload:
   1. Click **Add file**, then **Create new file**.
   2. Type the name `.github/workflows/au-watch.yml`.
   3. Paste in the contents of that file from the zip, then **Commit changes**.

## Step 3: Add your secrets
Go to **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Add one secret at a time:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | `au-llb-z8xp7v93es` (the topic already on your phone) |
| `MAIL_TO` | the email address that should receive alerts |

That's all you need. Email goes out through ntfy, with no Gmail password required.

These are optional:
- **PDFs attached to emails:** also add `GMAIL_USER` and `GMAIL_APP_PASS` (your Gmail and its 16-letter app password).
- **WhatsApp, once CallMeBot has space again:** add `WHATSAPP_PHONE` (for example `+919876543210`) and `CALLMEBOT_APIKEY`.

## Step 4: Allow it to save its history
Go to **Settings**, then **Actions**, then **General**. Under **Workflow permissions**, choose **Read and write permissions**, then **Save**.

## Step 5: Turn it on and test
1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **AU LLB watcher**, then **Run workflow**, choose mode **test**, and click **Run workflow**. Within about a minute you should get a test push and email. The first email may land in **Spam**; mark it **Not spam**.
3. Run it once more with mode **check**. This records what's already on the site, and you get "AU watcher is live".

After that it runs every 10 minutes on its own. You can close the laptop.

## Good to know
- **Run status:** each run shows in the **Actions** tab. A green tick means it worked; a red X means it failed, and clicking it shows why.
- **Timing:** GitHub sometimes starts scheduled runs 5–15 minutes late at busy times. That's still far faster than checking by hand.
- **If the AU site blocks GitHub's servers** (some Indian government sites block foreign traffic), you'll get a "Can't reach the AU admissions site" alert after 3 hours, and the 9 AM message will warn you.
- **If the 9 AM message stops coming**, something is wrong. Check the Actions tab.
- **To pause:** go to **Actions**, then **AU LLB watcher**, then **⋯**, then **Disable workflow**.
- **The `state.json` file** is the watcher's memory of what it has already seen. Don't delete it, or it will start over.
