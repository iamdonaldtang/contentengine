# assets/fonts/

放品牌字体文件（.ttf / .otf）。Dockerfile 在 build 时把本目录所有字体 COPY 进
/usr/share/fonts/taskon/ 并跑 fc-cache，渲染器（cairosvg / Pillow）即可按字体族名引用。

- 中文字体：系统已装 `fonts-noto-cjk`（族名 `Noto Sans CJK SC`），SVG 模板默认用它，
  无需在此放中文字体即可避免方块。
- 品牌英文字体：把 TaskOn 品牌字体文件放这里，并在 SVG 模板 font-family 里引用其族名。
- 🔴 待 Donald 拍板：TaskOn 品牌主字体文件 + 族名（拍板点①）。当前模板用 Noto Sans CJK SC +
  通用 sans-serif 兜底。

此目录可为空（.gitkeep 占位）；Dockerfile 的 cp 用 `|| true` 容忍空目录。
