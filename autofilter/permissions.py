class Permissions:
    def __init__(self, owners, db):
        self.owners, self.db = owners, db

    def root(self, uid):
        return uid in self.owners

    async def staff(self, uid):
        return self.root(uid) or await self.db.is_sudo(uid)

    async def search(self, uid, cid, kind, private_enabled):
        if kind == "private":
            return private_enabled or self.root(uid)
        return kind in ("group", "supergroup") and await self.db.approved(cid)
