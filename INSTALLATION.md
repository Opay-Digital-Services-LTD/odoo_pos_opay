# Installing OPay POS Terminal

This guide explains how to install `pos_opay` without using Git commands.

## Before Installation

- Confirm whether your server runs **Odoo 18** or **Odoo 19**.
- Have an active OPay merchant account.
- Have a compatible OPay physical POS terminal.
- Obtain the OPay integration credentials and terminal information.
- Back up the database before installing on an existing production system.

## Choose the Correct Download

Download the matching ZIP from the
[OPay POS Terminal GitHub Releases page](https://github.com/Opay-Digital-Services-LTD/odoo_pos_opay/releases):

- Odoo 18: `pos_opay-18.0.1.0.2.zip`
- Odoo 19: `pos_opay-19.0.1.0.2.zip`

Choose the named `pos_opay-...zip` file under **Assets**. Do not use GitHub's
automatically generated **Source code** ZIP, because it does not have the
required addon folder name.

Do not install an Odoo 18 package on Odoo 19, or an Odoo 19 package on Odoo 18.

After extraction, the module must have this structure:

```text
pos_opay/
    __manifest__.py
    __init__.py
    models/
    services/
    ...
```

If the ZIP creates an extra outer folder, use the inner `pos_opay` folder that
directly contains `__manifest__.py`.

## 1. Odoo On-Premise / Self-hosted

**Supported:** Yes, on the matching Odoo version.

You need access to the Odoo server files and permission to restart Odoo. You
also need an Odoo administrator account to install the module.

1. Download and extract the correct release ZIP.
2. Identify the server's **custom addons directory**. It is a directory already
   included in Odoo's `addons_path`. If you do not know it, ask the person who
   manages your Odoo server for the custom addons path.
3. Copy the complete `pos_opay` folder into that directory, alongside any other
   custom addon folders.
4. Restart the Odoo service so it can discover the new module.
5. Sign in to Odoo as an administrator.
6. Open **Apps**, choose **Update Apps List**, and confirm the update. Enable
   developer mode first if **Update Apps List** is not visible.
7. Search for **OPay POS Terminal** and select **Install**. If it is hidden by
   the default Apps filter, remove that filter or select the **Extra** filter.

## 2. Odoo.sh

**Supported:** Yes, for Odoo.sh projects that accept custom Python addons.

You cannot upload the ZIP directly into a running Odoo.sh database. The
recommended method is to connect this repository to the repository used by the
Odoo.sh project as a Git submodule. You need write access to the project
repository, access to the Odoo.sh project, and an Odoo administrator account.

1. In Odoo.sh, open the project and select a development or staging branch.
2. Choose **Submodule → Run**.
3. Enter the following values:

   - **Repository URL:**
     `git@github.com:Opay-Digital-Services-LTD/odoo_pos_opay.git`
   - **Branch:** `18.0` for Odoo 18, or `19.0` for Odoo 19
   - **Path:** `pos_opay`

4. Run the command, commit the generated submodule change, and push it to the
   project branch. Odoo.sh will create a new build.
5. Confirm that the connected project repository contains `.gitmodules` and a
   submodule directory named exactly `pos_opay`.
6. Wait for the build to finish, open its database, and sign in as an
   administrator.
7. Open **Apps → Update Apps List**, search for **OPay POS Terminal**, and select
   **Install**. Remove the default Apps filter if necessary.
8. Verify the module in staging before merging the branch into production.

The checkout directory must be named `pos_opay`. Do not use the GitHub
repository name `odoo_pos_opay` as the submodule path. Odoo uses the directory
name as the addon's technical name; an incorrect name prevents assets referenced
as `pos_opay/static/...` from loading and may result in a blank Odoo page.

The expected project layout is:

```text
.gitmodules
pos_opay/
    __manifest__.py
    __init__.py
    static/
    ...
```

A submodule records a specific repository revision. When a new `pos_opay`
version is released, update the submodule reference in the Odoo.sh project
repository and push that small change to create a new build. Do not keep a
manually uploaded second copy of `pos_opay` beside the submodule.

## 3. Local Odoo Development or Testing

**Supported:** Yes, on a matching local Odoo 18 or Odoo 19 setup.

You need access to the local Odoo files/configuration and an administrator login
for the test database.

1. Download and extract the package matching the local Odoo version.
2. Copy `pos_opay` into the local custom-addons directory.
3. Confirm that the custom-addons directory is included in `addons_path` in the
   Odoo configuration file.
4. Restart the local Odoo server.
5. Sign in as an administrator and open **Apps → Update Apps List**.
6. Search for **OPay POS Terminal** and select **Install**. Remove the default
   Apps filter if the module is not shown.

## After Installation

1. Open **Point of Sale → Configuration → Payment Methods**.
2. Create a payment method and select a **Bank** journal.
3. Set **Integration** to **Terminal** and **Integrate with** to **OPay**.
4. Enter the OPay Business ID, Branch ID, terminal serial number,
   `subSceneEnum`, client authentication key, OPay public key, and merchant
   private key supplied for the integration.
5. Save the payment method and assign it to the intended Point of Sale.
6. Start a POS session and confirm that OPay appears as a payment option.

## Troubleshooting

### The module is not visible

- Confirm the folder is named `pos_opay` and directly contains
  `__manifest__.py`.
- Remove the default Apps filter or select the **Extra** filter.
- Confirm the custom-addons directory is included in `addons_path`.
- Restart Odoo and run **Apps → Update Apps List** again.

### Odoo.sh opens a blank page after adding the repository

- Check `.gitmodules` and confirm the submodule path is exactly `pos_opay`.
- Confirm `pos_opay/__manifest__.py` exists in the project branch.
- Do not install it from a directory named `odoo_pos_opay`.
- After correcting the path and completing a new build, use a private browser
  window or refresh with `Ctrl+Shift+R` so old broken assets are not reused.

### Odoo reports an incompatible version

Confirm that the downloaded package matches the server's Odoo major version.
Do not rename or modify the package version to make it install.

### Odoo reports a missing Python package

The normal Odoo installation usually provides the required Python packages. If
Odoo reports one as missing, ask the server administrator to install that
dependency in the same Python environment used by Odoo, then restart Odoo.
