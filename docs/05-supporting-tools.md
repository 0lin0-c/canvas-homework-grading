# 辅助工具与演练原型设计

## 二维码解码工具

[decode_qr.py](../tools/decode_qr.py) 将指定PDF页面通过 Poppler 渲染后，以 zxing-cpp 解码，输出页号、码内容和位置。调用方仍需依据图像位置及“习题×答案”标签选对二维码。工具不访问登录资源、不获取Cookie。

依赖：Python、Pillow、zxing-cpp、Poppler 的 pdftoppm。

```sh
python -m pip install -r tools/requirements.txt
python tools/decode_qr.py "<QUESTION_BANK_PDF>" --page 5 --pdftoppm "<PDFTOPPM_EXECUTABLE>"
```

页号从1开始；文件不存在、页号非法、渲染失败或缺依赖时应报告失败。已对演练题库第5页测试，识别两个二维码，底部答案码需结合标签选择。

## 本地演练台

[grading-rehearsal.html](../prototypes/grading-rehearsal.html) 是自包含的虚构数据原型。模拟分数保存、独立评语提交、localStorage持久化、重复运行和冲突检测。没有Canvas接口、登录或真实数据。

```sh
python -m http.server 8766 --bind 127.0.0.1 --directory prototypes
```

打开 `http://127.0.0.1:8766/grading-rehearsal.html`，从样本选择开始。浏览器数据会跨刷新保留；新一轮从新的浏览器配置或清除该本地站点的测试数据开始。不要在演练台填入真实学生信息。

它只用于验证流程决策，不是Canvas保存接口的仿真服务。合成测试以事先标注的错误题号为输入，不能测量手写识别或数学判分准确率。

## 浏览器执行边界

正式网页读取和操作使用受支持的浏览器工具及当前文档，不通过脚本抓取认证信息或调用隐藏接口。文档不绑定历史标签ID、元素索引、临时预览地址或本机运行时路径。
