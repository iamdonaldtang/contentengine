# shared/js · 落地页 JS 引用说明

> **本目录只有说明文件 · 不存 JS 副本**。

## 1 · Master 文件位置

3 个落地页 JS 的 **master 版本** 在 engine 仓的 `frontend_snippets/` 目录下：

```
engine/frontend_snippets/
├── taskon_uid.js              ← 30 天 first-party cookie 持久化
├── landing_impression.js      ← onload 发 impression 浏览埋点
├── landing_form_submit.js     ← submit 发 lead 留资埋点
└── README.md                  ← 完整工程实施细则（前端读这个）
```

## 2 · 部署时如何使用

打包成 tar.gz 给运维时，**把 3 个 master JS 复制到部署目录的 `/js/`** 下：

```bash
# 假设你在 engine 仓根目录
mkdir -p build/free-diagnostic/js
cp frontend_snippets/taskon_uid.js \
   frontend_snippets/landing_impression.js \
   frontend_snippets/landing_form_submit.js \
   build/free-diagnostic/js/

# 然后打包 build/ 给运维
cd build && tar czf free-diagnostic-v1.0.0.tar.gz free-diagnostic/
```

运维部署后的目录结构：

```
/var/www/taskon.xyz/
├── free-diagnostic/
│   ├── index.html
│   └── styles.css
├── css/
│   └── taskon-base.css
└── js/
    ├── taskon_uid.js
    ├── landing_impression.js
    └── landing_form_submit.js
```

落地页 HTML 里通过 `<script src="/js/taskon_uid.js">` 引用，nginx 自动 serve。

## 3 · 为什么不在本目录放 JS 副本？

避免**版本漂移**：

- 如果在 `shared/js/` 下放 JS 副本，每次 `frontend_snippets/` 改了，要手动同步两处
- master 单一源，运维打包脚本 copy 一次，确保部署的总是最新版

## 4 · 升级流程

```
Day 1 · engine 维护者改 frontend_snippets/landing_form_submit.js
Day 2 · git commit + tag (e.g. snippets-v1.1.0)
Day 3 · 运行 build 脚本 copy 到 build/free-diagnostic/js/
Day 4 · 打 tar.gz → 发给 taskon 运维
Day 5 · 运维 sudo tar xzf 覆盖 → reload nginx
```

## 5 · 版本一致性检查

每次打包前，**验证 3 个文件版本一致**：

```bash
head -5 frontend_snippets/taskon_uid.js
head -5 frontend_snippets/landing_impression.js
head -5 frontend_snippets/landing_form_submit.js
```

各文件首注释行应有 `@version` 字段（如未实现，建议加上）。

## 6 · 紧急回滚

如果新版 JS 出问题：

```bash
# 找到上一个 tag
git tag --list | grep snippets-v

# 回退到上一个 tag
git checkout snippets-v1.0.0 -- frontend_snippets/

# 重新打包发给运维
```

---

## 7 · 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-15 | 首版 · 说明 JS master 在 frontend_snippets/ |
