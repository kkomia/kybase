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
        label: '列出知识库',
        summary: '列出当前 API Key 可访问的知识库。',
        args: '无',
        returns: '[{ id, name, documents, embedding_model }]',
        example: '{}',
        note: '返回每座库的名称、文档数与嵌入模型。',
      },
      {
        name: 'create_knowledge_base',
        label: '创建知识库',
        summary: '创建一个知识库，并绑定嵌入模型。',
        args: 'name* · description',
        returns: '{ id, name }',
        example: '{"name": "产品库", "description": "产品文档与手册"}',
        note: '嵌入模型在「模型」页登记后即可选择。',
      },
      {
        name: 'add_data_source',
        label: '添加数据源',
        summary: '接入 RSS 或网页来源。',
        args: 'knowledge_base_id* · kind(rss | html)* · url* · name',
        returns: '{ id, name, note }',
        example: '{"knowledge_base_id": "kb_9f3c", "kind": "html", "url": "https://example.com/guide"}',
        note: '来源内容会进入后续的解析与索引流程。',
      },
    ],
    ingest: [
      {
        name: 'upload_document',
        label: '上传文件',
        summary: '上传文件，异步解析并建立索引。',
        args: 'knowledge_base_id* · filename* · content_base64*',
        returns: '{ document_id, name, is_duplicate, note }',
        example: '{"knowledge_base_id": "kb_9f3c", "filename": "部署手册-NAS.pdf", "content_base64": "<base64>"}',
        note: 'MCP 单次 32 MB；REST 上传上限 200 MB。',
      },
      {
        name: 'get_document_status',
        label: '查看状态',
        summary: '查看文档的处理阶段与可检索状态。',
        args: 'document_id*',
        returns: '{ document_id, name, stage, chunks, error, searchable, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: '返回阶段、切块数、错误信息和 searchable。',
      },
      {
        name: 'reparse_document',
        label: '重新解析',
        summary: '重新解析文档并生成新的索引。',
        args: 'document_id*',
        returns: '{ document_id, name, stage, task_id, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: '适用于更换解析器或重新计算页码。',
      },
    ],
    manage: [
      {
        name: 'list_documents',
        label: '列出文档',
        summary: '列出知识库中的文档与状态。',
        args: 'knowledge_base_id* · query · limit',
        returns: '{ knowledge_base_id, total, documents[...], note }',
        example: '{"knowledge_base_id": "kb_9f3c", "limit": 20}',
        note: '支持按名称过滤与数量限制。',
      },
      {
        name: 'get_document_content',
        label: '读取内容',
        summary: '读取文档的 Markdown 或原件。',
        args: 'document_id* · format(markdown | original)',
        returns: '{ document_id, name, format, media_type, text | base64, download_url? }',
        example: '{"document_id": "doc_1c7e", "format": "markdown"}',
        note: '用 format 参数选择格式。',
      },
      {
        name: 'delete_document',
        label: '删除文档',
        summary: '删除文档，保留 7 天可恢复。',
        args: 'document_id*',
        returns: '{ document_id, trash_id, restorable_until, note }',
        example: '{"document_id": "doc_1c7e"}',
        note: '删除后切块与向量立即移除。',
      },
    ],
    retrieve: [
      {
        name: 'search',
        label: '检索',
        summary: '检索原文片段，返回判定、分布与出处。',
        args: 'query* · knowledge_base_ids · top_k · keep · min_score · per_doc · offset · filters',
        returns: '{ summary, distribution, decision, returned, offset, next_offset, hits[] }',
        example: '{"query": "嵌入模型怎么标定？", "knowledge_base_ids": ["kb_9f3c"], "keep": 4, "min_score": 0.531}',
        note: '支持 keep、min_score、per_doc 与分页参数。',
      },
      {
        name: 'get_document_pages',
        label: '按页读取',
        summary: '按页读取原文与页图。',
        args: 'document_id* · from_page · to_page*',
        returns: '{ total_pages, pages_reliable, pages[{ page, text, image_url? }] }',
        example: '{"document_id": "doc_1c7e", "from_page": 7, "to_page": 7}',
        note: '单次最多 50 页；PDF 和图片支持页图链接。',
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
      '<h3>一句话说明结果</h3><p>候选数量、高于基线的条数、收敛后保留的条数，以及建议参数。</p>',
    distribution:
      '<h3>命中分布与 fit</h3><p><code>fit</code> 分为 <code>strong</code>、<code>weak</code>、<code>none</code>、<code>unknown</code>。' +
      '基线按嵌入模型标定，BGE-M3 为 0.531。</p>',
    decision:
      '<h3>本次检索的参数</h3><p>记录 <code>keep</code>、<code>min_score</code>、<code>per_doc</code>、<code>decided_by</code>、' +
      '<code>dropped</code> 与 <code>unscored_kept</code>。</p>',
    hits:
      '<h3>原文片段</h3><p>每条包含 <code>text</code>、<code>page</code>、<code>heading_path</code>、<code>chunk_id</code>、' +
      '<code>basis</code> 与 <code>similarity</code>。</p>',
    next_offset:
      '<h3>继续翻页</h3><p>将 <code>next_offset</code> 作为下一次请求的 <code>offset</code>，直到返回 <code>null</code>。</p>',
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
    const switchButtons = tuning.querySelectorAll('[data-tune-mode]');
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
        text: '采用分布建议的参数。',
        count: '38 candidates · 4 survived',
        verdict: '资料吻合度高，按每篇最相关的结果收敛。',
      },
      custom: {
        keep: '6',
        min: '0.531',
        perDoc: '1',
        decided: 'model',
        code: '{"keep": 6, "min_score": 0.531, "per_doc": 1}',
        text: '每篇文档保留一条，keep 调整为 6。',
        count: '38 candidates · 6 survived',
        verdict: '每篇文档只保留最相关的一条。',
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
      switchButtons.forEach((button) => {
        const active = button.dataset.tuneMode === key;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      if (bars) {
        const spans = bars.querySelectorAll('span');
        spans.forEach((span, index) => span.classList.toggle('hot', index < (key === 'custom' ? 7 : 6) && index >= 4));
      }
    };

    switchButtons.forEach((button) => {
      button.addEventListener('click', () => renderTune(button.dataset.tuneMode === 'custom' ? 'custom' : 'suggested'));
    });

    modes.forEach((mode) => {
      mode.addEventListener('click', () => {
        modes.forEach((item) => item.classList.toggle('is-active', item === mode));
        if (!modeNote) return;
        const labels = {
          hybrid: '向量 + 词面，RRF 融合',
          vector: '仅向量检索',
          fulltext: '仅全文检索',
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
