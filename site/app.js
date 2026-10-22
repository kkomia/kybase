/* kybase 展示页 · 交互与动效
   —— 终端叙事 + 纸面证据。动效只承担反馈、引导与叙事；
      一行代码坏掉时，页面内容必须还在（最后有兜底）。 */

(() => {
  const root = document.documentElement;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const motionOff = /[?&]motion=off/.test(location.search);
  const hasGsap = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';
  const canAnimate = hasGsap && !reduceMotion && !motionOff;

  /* ---------------------------------------------------------------- 基础 UI */

  const topbar = document.querySelector('[data-topbar]');
  if (topbar) {
    const onScroll = () => topbar.classList.toggle('is-scrolled', window.scrollY > 8);
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  const menuToggle = document.getElementById('menu-toggle');
  const mobileNav = document.getElementById('mobile-nav');
  if (menuToggle && mobileNav) {
    const close = () => {
      menuToggle.setAttribute('aria-expanded', 'false');
      mobileNav.hidden = true;
    };
    menuToggle.addEventListener('click', () => {
      const open = menuToggle.getAttribute('aria-expanded') === 'true';
      menuToggle.setAttribute('aria-expanded', String(!open));
      mobileNav.hidden = open;
    });
    mobileNav.querySelectorAll('a').forEach((link) => link.addEventListener('click', close));
    window.addEventListener('resize', () => {
      if (window.innerWidth > 1020) close();
    });
  }

  /* 复制：按钮上直接说结果 */
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const card = button.closest('.code-card');
      const pre = card && card.querySelector('pre');
      if (!pre) return;
      const text = pre.innerText;
      try {
        await navigator.clipboard.writeText(text);
      } catch (error) {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        area.remove();
      }
      const old = button.textContent;
      button.textContent = '已复制';
      button.classList.add('is-done');
      window.setTimeout(() => {
        button.textContent = old;
        button.classList.remove('is-done');
      }, 1600);
    });
  });

  /* ---------------------------------------------------------------- 工具面 */

  const TOOL_GROUPS = {
    access: [
      {
        name: 'list_knowledge_bases',
        label: '列库',
        summary: '列出当前 API Key 能看到的库，带文档数与嵌入模型。',
        args: '无',
        returns: '[{ id, name, documents, embedding_model }]',
        example: '{}',
        note: '顶层是数组；Key 限了库范围时只回范围内的库。',
      },
      {
        name: 'create_knowledge_base',
        label: '建库',
        summary: '建一座新库，绑定一个已登记的嵌入模型。',
        args: 'name* · description',
        returns: '{ id, name }',
        example: '{"name": "产品库", "description": "产品文档与手册"}',
        note: '没登记嵌入模型时建库会被拒——服务不替你猜用哪个模型。',
      },
      {
        name: 'add_data_source',
        label: '挂来源',
        summary: '登记 RSS 或网页源，让它进入后续的抓取与摄入。',
        args: 'knowledge_base_id* · kind(rss | html)* · url* · name',
        returns: '{ id, name, note }',
        example: '{"knowledge_base_id": "kb_9f3c", "kind": "html", "url": "https://example.com/guide"}',
        note: '登记不等于立刻抓取；内容出现时再查文档状态。',
      },
    ],
    ingest: [
      {
        name: 'upload_document',
        label: '上传',
        summary: '传文件进库，异步摄入；重复文件直接告诉你。',
        args: 'knowledge_base_id* · filename* · content_base64*',
        returns: '{ document_id, name, is_duplicate, note }',
        example: '{"knowledge_base_id": "kb_9f3c", "filename": "部署手册-NAS.pdf", "content_base64": "<base64>"}',
        note: 'MCP 单次 32 MB，REST 上传上限 200 MB；is_duplicate 为 true 时不会入第二份。',
      },
      {
        name: 'get_document_status',
        label: '看进度',
        summary: '查一份文档走到哪一步，能不能搜。',
        args: 'document_id*',
        returns: '{ document_id, name, stage, chunks, error, searchable, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: 'searchable 按已落库切块算，不看阶段名；失败态能回到可重试的步骤。',
      },
      {
        name: 'reparse_document',
        label: '重解析',
        summary: '清掉旧产物，从解析器重跑。换解析器或修页码时用它。',
        args: 'document_id*',
        returns: '{ document_id, name, stage, task_id, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: '写操作，权限同删除；会重新消耗云端解析额度。',
      },
    ],
    manage: [
      {
        name: 'list_documents',
        label: '列文档',
        summary: '按库列文档与状态，可按名称过滤。',
        args: 'knowledge_base_id* · query · limit',
        returns: '{ knowledge_base_id, total, documents[...], note }',
        example: '{"knowledge_base_id": "kb_9f3c", "limit": 20}',
        note: '每项带 stage、searchable、disabled——管理动作在会话里就能做。',
      },
      {
        name: 'get_document_content',
        label: '取全文',
        summary: '取解析产物或原件，两种格式随时切换。',
        args: 'document_id* · format(markdown | original)',
        returns: '{ document_id, name, format, media_type, text | base64, download_url? }',
        example: '{"document_id": "doc_1c7e", "format": "markdown"}',
        note: 'download_url 需要部署同时配好对外基址与签名密钥，缺一个就不给这一项。',
      },
      {
        name: 'delete_document',
        label: '删除',
        summary: '把文档移进回收站，7 天内可恢复。',
        args: 'document_id*',
        returns: '{ document_id, trash_id, restorable_until, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: '切块与向量立即清除，删完立刻搜不到。',
      },
    ],
    retrieve: [
      {
        name: 'search',
        label: '检索',
        summary: '混合检索返回带出处的原文片段，不是生成好的回答。',
        args: 'query* · knowledge_base_ids · top_k · keep · min_score · per_doc · offset · filters',
        returns: '{ summary, distribution, decision, returned, offset, next_offset, hits[] }',
        example: '{"query": "嵌入模型怎么标定？", "knowledge_base_ids": ["kb_9f3c"], "keep": 4, "min_score": 0.531}',
        note: '先读 summary / fit；命中太多就调 keep / min_score / per_doc 再查一次。',
      },
      {
        name: 'get_document_pages',
        label: '按页取',
        summary: '取一份文档某几页的原文；PDF 与图片类还给页图链接。',
        args: 'document_id* · from_page · to_page*',
        returns: '{ total_pages, pages_reliable, pages[{ page, text, image_url? }] }',
        example: '{"document_id": "doc_1c7e", "from_page": 7, "to_page": 7}',
        note: '一次最多 50 页；pages_reliable 为 false 时，别拿它当「第 N 页」引用。',
      },
    ],
  };

  const toolExplorer = document.querySelector('[data-tools]');
  if (toolExplorer) {
    const tabs = toolExplorer.querySelectorAll('.tool-tab');
    const indicator = toolExplorer.querySelector('.tool-indicator');
    const list = toolExplorer.querySelector('[data-tool-list]');
    const detail = toolExplorer.querySelector('[data-tool-detail]');
    let currentGroup = 'access';
    let currentTool = 0;

    const moveIndicator = () => {
      const active = toolExplorer.querySelector('.tool-tab.is-active');
      if (!active || !indicator) return;
      indicator.style.width = active.offsetWidth + 'px';
      indicator.style.transform = 'translateX(' + active.offsetLeft + 'px)';
    };

    const renderDetail = () => {
      const tools = TOOL_GROUPS[currentGroup];
      const tool = tools[currentTool] || tools[0];
      if (!tool || !detail) return;
      detail.innerHTML =
        '<div class="td-top"><span class="td-name">' + tool.name + '</span><span class="td-kind">' +
        tool.label + '</span></div>' +
        '<p class="td-summary">' + tool.summary + '</p>' +
        '<div class="td-cols"><div class="td-col"><span class="td-col-label">入参</span><p>' + tool.args +
        '</p></div><div class="td-col"><span class="td-col-label">返回</span><p>' + tool.returns + '</p></div></div>' +
        '<pre class="td-code"><code>{ "name": "' + tool.name + '", "arguments": ' + tool.example + ' }</code></pre>' +
        '<p class="td-note">' + tool.note + '</p>';
    };

    const renderList = () => {
      if (!list) return;
      const tools = TOOL_GROUPS[currentGroup];
      list.innerHTML = tools
        .map(
          (tool, index) =>
            '<li class="' + (index === currentTool ? 'is-active' : '') + '"><button type="button" data-tool="' +
            index + '"><span class="tname">' + tool.name + '</span><span class="tarrow">→</span><span class="tlabel">' +
            tool.label + '</span></button></li>'
        )
        .join('');
      list.querySelectorAll('button').forEach((button) => {
        button.addEventListener('click', () => {
          currentTool = Number(button.dataset.tool);
          list.querySelectorAll('li').forEach((li, index) => li.classList.toggle('is-active', index === currentTool));
          renderDetail();
        });
      });
    };

    const selectGroup = (group) => {
      currentGroup = group;
      currentTool = 0;
      tabs.forEach((tab) => {
        const active = tab.dataset.group === group;
        tab.classList.toggle('is-active', active);
        tab.setAttribute('aria-selected', String(active));
      });
      moveIndicator();
      renderList();
      renderDetail();
    };

    tabs.forEach((tab) => tab.addEventListener('click', () => selectGroup(tab.dataset.group)));
    window.addEventListener('resize', moveIndicator);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(moveIndicator).catch(() => {});
    }
    renderList();
    renderDetail();
    window.requestAnimationFrame(moveIndicator);
  }

  /* ---------------------------------------------------------------- 返回说明 */

  const NOTE_CONTENT = {
    summary:
      '<h3>先给判定，再给原文</h3><p>一句话说清候选多少、高于基线几条、收敛后留几条，以及收敛参数是谁建议的。' +
      '<code>fit=none</code> 时它会明确说「资料大概答不了这个问题」，MCP 载荷另带一句 <code>note</code>。</p>',
    distribution:
      '<h3>分布和 fit 在同一次返回里</h3><p><code>fit</code> 是规则算出来的：<code>max</code> 低于基线是 <code>none</code>；' +
      '<code>max</code> ≥ 基线 + 0.095 且 <code>median</code> ≥ 基线是 <code>strong</code>；其余 <code>weak</code>。' +
      '没标定过的模型给 <code>unknown</code>，不自动收敛。基线按嵌入模型标定（bge-m3 为 0.531）。</p>',
    decision:
      '<h3>「为什么只回了 3 条」要有答案</h3><p>记录本次实际生效的 <code>keep</code> / <code>min_score</code> / <code>per_doc</code>、' +
      '谁给的 <code>decided_by</code>、丢掉多少 <code>dropped</code>，以及绕过阈值的词面命中 <code>unscored_kept</code>。</p>',
    hits:
      '<h3>原文切片，不是摘要</h3><p>每条带 <code>text</code>、<code>page</code>、<code>heading_path</code>、<code>chunk_id</code>、' +
      '<code>basis</code>、<code>similarity</code> 与 <code>lexical_score</code>。<code>page</code> 为 null 是如实的「没有可信页码」，不是缺字段。</p>',
    next_offset:
      '<h3>翻页不改判定</h3><p><code>offset</code> 只切「幸存者」名单；<code>summary</code> / <code>distribution</code> / <code>decision</code> 在每一页逐字相同。' +
      '把 <code>next_offset</code> 填回 <code>offset</code> 继续，直到它变成 <code>null</code>。</p>',
  };

  const answer = document.querySelector('[data-answer]');
  if (answer) {
    const noteTabs = answer.querySelectorAll('.note-tab');
    const noteBody = answer.querySelector('[data-note-body]');
    const blocks = answer.querySelectorAll('.pblock');
    const setNote = (key) => {
      noteTabs.forEach((tab) => {
        const active = tab.dataset.note === key;
        tab.classList.toggle('is-active', active);
        tab.setAttribute('aria-selected', String(active));
      });
      blocks.forEach((block) => block.classList.toggle('is-active', block.dataset.block === key));
      if (noteBody) noteBody.innerHTML = NOTE_CONTENT[key] || '';
    };
    noteTabs.forEach((tab) => tab.addEventListener('click', () => setNote(tab.dataset.note)));
    blocks.forEach((block) => {
      block.addEventListener('click', () => {
        if (NOTE_CONTENT[block.dataset.block]) setNote(block.dataset.block);
      });
    });
    setNote('distribution');
  }

  /* ---------------------------------------------------------------- 双格式 */

  const formats = document.querySelector('[data-formats]');
  if (formats) {
    const buttons = formats.querySelectorAll('[data-format]');
    const panes = formats.querySelectorAll('[data-pane]');
    const select = (key) => {
      buttons.forEach((button) => {
        const active = button.dataset.format === key;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-selected', String(active));
      });
      panes.forEach((pane) => {
        const active = pane.dataset.pane === key;
        pane.hidden = !active;
        pane.classList.toggle('is-active', active);
      });
    };
    buttons.forEach((button) => button.addEventListener('click', () => select(button.dataset.format)));
  }

  /* ---------------------------------------------------------------- 调参 */

  const tuning = document.querySelector('[data-tuning]');
  if (tuning) {
    const toggle = tuning.querySelector('[data-tune-toggle]');
    const keep = tuning.querySelector('[data-keep]');
    const min = tuning.querySelector('[data-min]');
    const perDoc = tuning.querySelector('[data-perdoc]');
    const decided = tuning.querySelector('[data-decided]');
    const code = tuning.querySelector('[data-code]');
    const text = tuning.querySelector('[data-text]');
    const count = tuning.querySelector('[data-count]');
    const verdict = tuning.querySelector('[data-verdict]');
    const bars = tuning.querySelector('[data-bars]');
    const modes = tuning.querySelectorAll('[data-mode]');
    const modeNote = tuning.querySelector('.tune-row .tr-note');

    const STATE = {
      suggested: {
        keep: '4',
        min: '0.531',
        perDoc: 'null',
        decided: 'suggested',
        code: '{"keep": 4, "min_score": 0.531, "per_doc": null}',
        text: '按分布建议收。三个旋钮原样带回，翻页时判定不变。',
        count: '38 candidates · 4 survived',
        verdict: '资料很对得上，按每篇最相关的收敛。',
      },
      custom: {
        keep: '6',
        min: '0.531',
        perDoc: '1',
        decided: 'model',
        code: '{"keep": 6, "min_score": 0.531, "per_doc": 1}',
        text: 'Agent 把 per_doc 调成 1：跨文档各留一条，避免同一本书刷屏；keep 同步抬到 6。',
        count: '38 candidates · 6 survived',
        verdict: '分布没变；变的是这次怎么收——同一批候选，换一个读法。',
      },
    };

    const renderTune = (key) => {
      const state = STATE[key];
      if (keep) keep.textContent = state.keep;
      if (min) min.textContent = state.min;
      if (perDoc) perDoc.textContent = state.perDoc;
      if (decided) decided.textContent = state.decided;
      if (code) code.textContent = state.code;
      if (text) text.textContent = state.text;
      if (count) count.textContent = state.count;
      if (verdict) verdict.textContent = state.verdict;
      if (toggle) toggle.setAttribute('aria-pressed', String(key === 'custom'));
      if (bars) {
        const spans = bars.querySelectorAll('span');
        spans.forEach((span, index) => span.classList.toggle('hot', index < (key === 'custom' ? 7 : 6) && index >= 4));
      }
    };

    if (toggle) {
      toggle.addEventListener('click', () => {
        const next = toggle.getAttribute('aria-pressed') === 'true' ? 'suggested' : 'custom';
        renderTune(next);
      });
    }

    modes.forEach((mode) => {
      mode.addEventListener('click', () => {
        modes.forEach((item) => item.classList.toggle('is-active', item === mode));
        if (!modeNote) return;
        const labels = {
          hybrid: '向量 + 词面，RRF 融合',
          vector: '只走向量通道；min_score 切这一路',
          fulltext: '只走词面通道；min_score 不切它',
        };
        modeNote.textContent = labels[mode.dataset.mode] || labels.hybrid;
      });
    });
  }

  /* ---------------------------------------------------------------- 动效 */

  if (!canAnimate) {
    root.classList.remove('motion-ready');
    return;
  }

  gsap.registerPlugin(ScrollTrigger);
  window.addEventListener('error', () => root.classList.remove('motion-ready'), { once: true });
  gsap.set('.display .line span', { yPercent: 112 });

  const intro = gsap.timeline({ defaults: { ease: 'power3.out' }, delay: 0.05 });
  intro
    .to('.display .line span', { yPercent: 0, duration: 0.95, stagger: 0.09 }, 0.05)
    .to('.hero-side', { opacity: 1, y: 0, duration: 0.7 }, 0.34)
    .to('.session', { opacity: 1, y: 0, duration: 0.8 }, 0.44)
    .from('.msg', { opacity: 0, y: 8, duration: 0.32, stagger: 0.055 }, 0.7)
    .from('.hit', { opacity: 0, y: 12, duration: 0.5 }, 1.16)
    .add(() => root.classList.add('is-ready'), 1.7);

  gsap.set('.hero-side', { y: 18 });
  gsap.set('.session', { y: 28 });

  /* 区块出场：一次滚动触发，不重播 */
  const revealTargets = gsap.utils.toArray(
    [
      '.section:not(.hero) .section-head > *',
      '.tool-explorer',
      '.tool-foot',
      '.answer-layout .payload',
      '.answer-notes',
      '.format-switch',
      '.format-viewer',
      '.tune-chart',
      '.tune-panel',
      '.tune-foot',
      '.stack-route',
      '.provider',
      '.code-card',
      '.deploy-steps li',
      '.deploy-foot',
      '.section-note',
      '.cta-inner > *',
    ].join(',')
  );
  if (revealTargets.length) {
    gsap.set(revealTargets, { opacity: 0, y: 22 });
    ScrollTrigger.batch(revealTargets, {
      start: 'top 88%',
      once: true,
      onEnter: (batch) => gsap.to(batch, { opacity: 1, y: 0, duration: 0.65, stagger: 0.06, ease: 'power3.out' }),
    });
  }

  /* 页码叙事：滚动位置 1:1 驱动三段 */
  const narrative = document.querySelector('[data-narrative]');
  if (narrative && window.innerWidth > 1020) {
    const stageItems = narrative.querySelectorAll('[data-stage-item]');
    const chapters = narrative.querySelectorAll('[data-chapter]');
    const dots = narrative.querySelectorAll('.narrative-dots i');
    let step = -1;
    const setStep = (index) => {
      if (index === step) return;
      step = index;
      stageItems.forEach((item, i) => item.classList.toggle('is-active', i === index));
      chapters.forEach((item, i) => item.classList.toggle('is-active', i === index));
      dots.forEach((dot, i) => dot.classList.toggle('on', i === index));
    };
    setStep(0);
    ScrollTrigger.create({
      trigger: narrative,
      start: 'top top+=62',
      end: 'bottom bottom',
      onUpdate: (self) => {
        const progress = self.progress;
        setStep(progress < 0.3 ? 0 : progress < 0.56 ? 1 : 2);
      },
    });
  }

  /* 调参时数字和柱子一起变；失败也不挡住内容 */
  if (typeof ScrollTrigger !== 'undefined') {
    ScrollTrigger.refresh();
  }
})();
