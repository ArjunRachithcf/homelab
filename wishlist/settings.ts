// wishlist/settings.ts: Wishlist's settings from compose, written into its own database (wishlist/README.md).
// Wishlist reads no setting from its environment: sign-up, password login and OIDC live in its table
// system_config, edited in its admin UI. Mounted as prisma/homelab-settings.ts (beside upstream's client.ts) and
// run with the image's own tsx and Prisma client:
//   (no argument)  by wishlist/start.sh at every start, after the migrations, before Wishlist listens
//   status         prints the enforced settings (never the client secret) and "ok" when they hold
//   admin LOGIN    makes the user LOGIN (Authentik's username) an admin; LOGIN must have signed in once
import { prisma } from "./client";

const GLOBAL = "global";
const ADMIN_ROLE = 2;

function need(name: string): string {
    const value = process.env[name];
    if (!value) throw new Error(`${name} is not set`);
    return value;
}

// The household's rules: put back at every start, whatever the admin UI changed
function enforced(defaultGroup: string): Record<string, string> {
    return {
        enableSignup: "false",
        "security.disablePasswordLogin": "true",
        "oidc.enable": "true",
        "oidc.discoveryUrl": need("OIDC_DISCOVERY_URL"),
        "oidc.clientId": need("OIDC_CLIENT_ID"),
        "oidc.clientSecret": need("OIDC_CLIENT_SECRET"),
        "oidc.providerName": need("OIDC_PROVIDER_NAME"),
        "oidc.autoRedirect": "true",
        "oidc.autoRegister": "true",
        "oidc.enableSync": "true",
        // claims-hidden.patch hides claims from the owner only while this is off
        "claims.showForOwner": "false",
        // Off until a kid's public list can need an adult's approval
        allowPublicLists: "false",
        // Without it, everyone after the first sign-in joins no group and gets no list
        defaultGroup
    };
}

// Set once; the admin may change them (the import switches suggestions to "auto" for a while)
const initial: Record<string, string> = {
    "suggestions.enable": "true",
    "suggestions.method": "approval"
};

async function defaultGroup(): Promise<string> {
    // The group upstream's seed creates ("Default"), or the oldest one
    const group =
        (await prisma.group.findFirst({ where: { name: "Default" }, select: { id: true } })) ??
        (await prisma.group.findFirst({ select: { id: true } }));
    if (!group) throw new Error("no group: upstream's seed should have created one");
    return group.id;
}

// While Wishlist has no user at all, its /login sends everyone to the setup wizard (the first account = admin) before
// it looks at an OIDC callback, so the first sign-in through Authentik could never finish. An inactive placeholder,
// like Authentik's akadmin, keeps it out of that state: no password (and password login is off), no Authentik link,
// no group, no list; a plain member, never admin.
const PLACEHOLDER = { username: "homelab-placeholder", name: "Placeholder", email: "placeholder@homelab.invalid" };

async function placeholder() {
    if (await prisma.user.findUnique({ where: { username: PLACEHOLDER.username }, select: { id: true } })) return;
    await prisma.user.create({ data: { ...PLACEHOLDER, hashedPassword: "!" } });
    console.log("homelab placeholder user created");
}

async function current(): Promise<Record<string, string | null>> {
    const rows = await prisma.systemConfig.findMany({ where: { groupId: GLOBAL } });
    return Object.fromEntries(rows.map((r) => [r.key, r.value]));
}

async function apply() {
    await placeholder();
    const wanted = enforced(await defaultGroup());
    const have = await current();
    for (const [key, value] of Object.entries(initial)) {
        if (!(key in have)) wanted[key] = value;
    }
    for (const [key, value] of Object.entries(wanted)) {
        await prisma.systemConfig.upsert({
            where: { key_groupId: { key, groupId: GLOBAL } },
            create: { key, groupId: GLOBAL, value },
            update: { value }
        });
    }
    console.log(`homelab settings applied (${Object.keys(wanted).length} keys)`);
}

async function status() {
    const wanted = enforced(await defaultGroup());
    const have = await current();
    let ok = true;
    for (const [key, value] of Object.entries(wanted)) {
        const holds = have[key] === value;
        ok &&= holds;
        const shown = key === "oidc.clientSecret" ? (have[key] ? "(set)" : "(missing)") : have[key];
        console.log(`${holds ? "ok  " : "DIFF"} ${key} = ${shown}`);
    }
    for (const key of Object.keys(initial)) console.log(`     ${key} = ${have[key]}`);
    const users = await prisma.user.count({ where: { username: { not: PLACEHOLDER.username } } });
    const admins = await prisma.user.count({ where: { roleId: ADMIN_ROLE } });
    const held = (await prisma.user.count({ where: { username: PLACEHOLDER.username } })) === 1;
    ok &&= held;
    console.log(`${held ? "ok  " : "DIFF"} placeholder user ${PLACEHOLDER.username}`);
    console.log(`     users ${users}, admins ${admins}`);
    if (ok) console.log("ok");
}

async function admin(login: string | undefined) {
    if (!login) throw new Error("usage: admin LOGIN");
    const user = await prisma.user.findUnique({ where: { username: login }, select: { id: true } });
    if (!user) throw new Error(`no user ${login}: sign in once first`);
    await prisma.user.update({ where: { id: user.id }, data: { roleId: ADMIN_ROLE } });
    console.log(`${login} is an admin`);
}

const [command, arg] = process.argv.slice(2);
const run = command === "status" ? status() : command === "admin" ? admin(arg) : apply();
run.then(
    () => prisma.$disconnect(),
    async (e) => {
        console.error(`homelab-settings: ${e instanceof Error ? e.message : e}`);
        await prisma.$disconnect();
        process.exit(1);
    }
);
