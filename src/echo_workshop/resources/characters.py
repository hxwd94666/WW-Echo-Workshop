# 从已验证的游戏目录投影角色属性，评分权重仍由 XWUID 管理。
def project_elements(templates, catalog):
    ids = tuple(cid for cid in templates if cid.isdigit())
    elements = {cid: catalog.characters[cid]['element'] for cid in ids if cid in catalog.characters}
    return elements, tuple(cid for cid in ids if cid not in elements)
