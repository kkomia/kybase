/* kybase 展示页 · 交互与动效
   原则：动效只承担反馈、引导与叙事；一行代码坏掉时，页面内容必须还在。 */

(() => {
  const root = document.documentElement;
  const params = new URLSearchParams(window.location.search);
  const forceReduced = params.get('motion') === 'off';
  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const reduced = forceReduced || prefersReduced;
  const gsap = window.gsap && !reduced ? window.gsap : null;
  const ScrollTrigger = window.ScrollTrigger;
  const canAnimate = Boolean(gsap && ScrollTrigger);

  if (canAnimate) {
    gsap.registerPlugin(ScrollTrigger);
    root.classList.add('motion-ready');
  } else {
    // GSAP 没加载或被禁用：把内联脚本先立起来的初始态撤掉，内容必须可见
    root.classList.remove('motion-ready');
    root.classList.add('is-static');
  }

  if (params.get('smooth') === 'off') root.classList.add('no-smooth');

  /* ---------------------------------------------------------------- 主题 */
  const themeButton = document.getElementById('theme-toggle');
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  const THEME_KEY = 'kybase-site-theme';

  const storedTheme = (() => {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch (error) {
      return null;
    }
  })();

  const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const initialTheme = storedTheme || (systemDark ? 'dark' : 'light');

  const themedShots = Array.from(document.querySelectorAll('[data-img-light][data-img-dark]'));

  function applyTheme(theme) {
    root.dataset.theme = theme;
    if (themeMeta) themeMeta.setAttribute('content', theme === 'dark' ? '#0d0e10' : '#f2efe9');
    themedShots.forEach((img) => {
      const next = theme === 'dark' ? img.dataset.imgDark : img.dataset.imgLight;
      if (next && img.getAttribute('src') !== next) img.setAttribute('src', next);
    });
    if (themeButton) {
      const next = theme === 'dark' ? '切换为浅色' : '切换为深色';
      themeButton.setAttribute('aria-label', next);
      themeButton.setAttribute('title', next);
    }
  }

  applyTheme(initialTheme);

  if (themeButton) {
    themeButton.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (error) {
        /* 隐私模式里存不了，主题仍然生效到本次会话 */
      }
    });
  }

  /* ---------------------------------------------------------------- 顶栏 */
  const topbar = document.querySelector('[data-topbar]');
  const onScroll = () => {
    if (topbar) topbar.classList.toggle('is-scrolled', window.scrollY > 8);
  };
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });

  /* 窄屏目录：没有横向导航时，从这里进五个分区 */
  const menuToggle = document.getElementById('menu-toggle');
  const mobileNav = document.getElementById('mobile-nav');

  if (menuToggle && mobileNav) {
    const closeMenu = () => {
      mobileNav.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
    };
    menuToggle.addEventListener('click', () => {
      const open = mobileNav.hidden;
      mobileNav.hidden = !open;
      menuToggle.setAttribute('aria-expanded', String(open));
    });
    mobileNav.addEventListener('click', (event) => {
      if (event.target.closest('a')) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !mobileNav.hidden) {
        closeMenu();
        menuToggle.focus();
      }
    });
    window.addEventListener('resize', () => {
      if (window.innerWidth > 860) closeMenu();
    });
  }

  /* ------------------------------------------------- 首屏判定卡片的演示 */
  const demo = document.querySelector('[data-demo]');
  const queryText = demo && demo.querySelector('.query-text');
  const dist = demo && demo.querySelector('[data-dist]');
  const verdict = demo && demo.querySelector('.verdict');
  const hits = demo && demo.querySelector('[data-hits]');
  const fitStamp = demo && demo.querySelector('[data-fit]');
  const demoFoot = demo && demo.querySelector('.decision-foot');
  const replay = demo && demo.querySelector('[data-replay]');
  const QUERY = '嵌入模型怎么标定？';
  const DIST = [14, 18, 24, 30, 36, 44, 52, 66, 84, 96, 72, 58, 46, 38, 30, 26, 22, 18, 15, 12, 9];

  let demoTimeline = null;
  let typingTimer = null;

  if (dist) {
    const bars = [];
    DIST.forEach((height) => {
      const bar = document.createElement('span');
      bar.style.setProperty('--h', `${height}%`);
      dist.appendChild(bar);
      bars.push(bar);
    });
    const peak = DIST.indexOf(Math.max(...DIST));
    if (bars[peak]) bars[peak].classList.add('is-peak');
  }

  /* 静态直方图也只亮一根峰：有数据含义的蓝，不是装饰 */
  document.querySelectorAll('.hits-dist').forEach((chart) => {
    const bars = Array.from(chart.children);
    if (!bars.length) return;
    let peak = 0;
    bars.forEach((bar, index) => {
      const height = parseFloat(bar.style.getPropertyValue('--h')) || 0;
      const best = parseFloat(bars[peak].style.getPropertyValue('--h')) || 0;
      if (height > best) peak = index;
    });
    bars[peak].classList.add('is-peak');
  });

  function stopTyping() {
    if (typingTimer) {
      window.clearInterval(typingTimer);
      typingTimer = null;
    }
  }

  function showStaticDemo() {
    stopTyping();
    if (queryText) queryText.textContent = QUERY;
    [verdict, hits, demoFoot].forEach((node) => {
      if (node) node.style.opacity = '1';
    });
    if (fitStamp) fitStamp.style.opacity = '1';
    if (demo) demo.classList.add('is-done');
  }

  function playDemo() {
    if (!demo || !queryText) return;
    stopTyping();
    if (demoTimeline) demoTimeline.kill();

    if (!canAnimate) {
      showStaticDemo();
      return;
    }

    demo.classList.remove('is-done');
    queryText.textContent = '';
    gsap.set(verdict, { opacity: 0 });
    gsap.set(hits, { opacity: 0 });
    gsap.set(demoFoot, { opacity: 0 });

    const chars = Array.from(QUERY);
    let index = 0;
    typingTimer = window.setInterval(() => {
      index += 1;
      queryText.textContent = chars.slice(0, index).join('');
      if (index >= chars.length) {
        stopTyping();
        startVerdictTimeline();
      }
    }, 62);

    function startVerdictTimeline() {
      demoTimeline = gsap.timeline({ delay: 0.24 });
      demoTimeline
        .to(verdict, { opacity: 1, duration: 0.28 }, 0)
        .fromTo(
          dist ? dist.children : [],
          { scaleY: 0 },
          { scaleY: 1, duration: 0.55, stagger: 0.018, ease: 'power3.out' },
          0.05
        )
        .fromTo(
          fitStamp,
          { scale: 1.5, opacity: 0, rotation: 7 },
          { scale: 1, opacity: 1, rotation: -2.5, duration: 0.42, ease: 'back.out(2.4)' },
          0.3
        )
        .to(hits, { opacity: 1, duration: 0.18 }, 0.42)
        .fromTo(
          hits ? hits.children : [],
          { y: 10, opacity: 0 },
          { y: 0, opacity: 1, duration: 0.42, stagger: 0.07, ease: 'power2.out' },
          0.44
        )
        .to(demoFoot, { opacity: 1, duration: 0.3 }, 0.72)
        .add(() => demo.classList.add('is-done'));
    }
  }

  if (demo) {
    if (canAnimate) {
      gsap.set(verdict, { opacity: 0 });
      gsap.set(hits, { opacity: 0 });
      gsap.set(demoFoot, { opacity: 0 });
      window.setTimeout(playDemo, 520);
    } else {
      showStaticDemo();
    }
    if (replay) replay.addEventListener('click', playDemo);
  }

  /* ---------------------------------------------------------- 揭幕与叙事 */
  if (canAnimate) {
    const mm = gsap.matchMedia();

    /* 首屏：标题分行升起，判定卡片后到。
       注意 y 与 yPercent 要一起给：样式表里的 translateY(112%) 会被 GSAP
       读成像素 y，只动 yPercent 的话动画结束会落回 112%。 */
    const heroIntro = gsap.timeline({ defaults: { ease: 'power3.out' } });
    heroIntro
      .set('.display .line > span', { y: 0, yPercent: 112 })
      .to('.display .line > span', { y: 0, yPercent: 0, duration: 0.95, stagger: 0.1 }, 0.12)
      .from('.lede', { y: 18, opacity: 0, duration: 0.7 }, 0.4)
      .from('.actions > *', { y: 12, opacity: 0, duration: 0.5, stagger: 0.08 }, 0.5)
      .from('.hero-facts > div', { y: 10, opacity: 0, duration: 0.5, stagger: 0.06 }, 0.58)
      .from('.decision', { y: 28, opacity: 0, duration: 0.85 }, 0.28);

    /* 主张：逐字亮起 */
    const manifesto = document.querySelector('.manifesto-line');
    if (manifesto) {
      const text = manifesto.textContent.trim();
      manifesto.textContent = '';
      Array.from(text).forEach((char) => {
        const span = document.createElement('span');
        span.className = 'word';
        span.textContent = char;
        manifesto.appendChild(span);
      });
      gsap.to(manifesto.querySelectorAll('.word'), {
        opacity: 1,
        y: 0,
        duration: 0.5,
        stagger: 0.035,
        ease: 'power2.out',
        scrollTrigger: { trigger: manifesto, start: 'top 78%', once: true },
      });
      gsap.set(manifesto.querySelectorAll('.word'), { y: 12 });
    }

    /* 分区标题与行 */
    gsap.utils.toArray('.section-head').forEach((head) => {
      gsap.from(head.children, {
        y: 24,
        opacity: 0,
        duration: 0.75,
        stagger: 0.08,
        ease: 'power3.out',
        scrollTrigger: { trigger: head, start: 'top 84%', once: true },
      });
    });

    gsap.utils.toArray('[data-reveal]').forEach((node) => {
      gsap.from(node, {
        y: 26,
        opacity: 0,
        duration: 0.75,
        ease: 'power2.out',
        scrollTrigger: { trigger: node, start: 'top 86%', once: true },
      });
    });

    gsap.utils.toArray('.plate').forEach((plate) => {
      gsap.from(plate, {
        y: 34,
        opacity: 0,
        duration: 0.9,
        ease: 'power2.out',
        scrollTrigger: { trigger: plate, start: 'top 88%', once: true },
      });
    });

    /* 吸附叙述：只有在宽屏且未要求减少动态时启用 */
    mm.add('(min-width: 1081px)', () => {
      const narrative = document.querySelector('[data-narrative]');
      if (!narrative) return;

      const sheet = narrative.querySelector('[data-sheet]');
      const chunks = narrative.querySelector('[data-chunks]');
      const hitsStage = narrative.querySelector('[data-hits-stage]');
      const hitRows = hitsStage ? Array.from(hitsStage.querySelectorAll('.hit-row')) : [];
      const quote = hitsStage ? hitsStage.querySelector('.hit-quote') : null;
      const chapters = Array.from(narrative.querySelectorAll('[data-chapter]'));
      const steps = Array.from(narrative.querySelectorAll('[data-step]'));
      const progressBar = narrative.querySelector('[data-progress]');

      if (!sheet || !chunks || !hitsStage) return;

      gsap.set(chunks, { autoAlpha: 0, y: 26 });
      gsap.set(hitsStage, { autoAlpha: 0 });
      gsap.set(hitRows, { autoAlpha: 0, y: 10 });
      gsap.set(quote, { autoAlpha: 0, y: 14 });
      gsap.set(chapters.slice(1), { autoAlpha: 0, y: 14 });

      const timeline = gsap.timeline({
        defaults: { ease: 'none' },
        scrollTrigger: {
          trigger: narrative,
          start: 'top top',
          end: 'bottom bottom',
          scrub: 0.6,
          onUpdate: (self) => {
            const progress = self.progress;
            if (progressBar) gsap.set(progressBar, { scaleY: progress });
            const thresholds = [0, 0.2, 0.52, 0.82];
            let current = 0;
            thresholds.forEach((threshold, i) => {
              if (progress >= threshold) current = i;
            });
            steps.forEach((step, i) => step.classList.toggle('is-active', i === current));
          },
        },
      });

      timeline
        .to(sheet, { autoAlpha: 0, y: -24, scale: 0.985, duration: 0.06 }, 0.16)
        .to(chunks, { autoAlpha: 1, duration: 0.03 }, 0.2)
        .to(chunks.children, { autoAlpha: 1, y: 0, duration: 0.05, stagger: 0.01 }, 0.2)
        .to(chapters[0], { autoAlpha: 0, y: -12, duration: 0.03 }, 0.14)
        .to(chapters[1], { autoAlpha: 1, y: 0, duration: 0.03 }, 0.2)
        .to(chunks, { autoAlpha: 0, y: -16, duration: 0.05 }, 0.5)
        .to(chunks.children, { autoAlpha: 0, duration: 0.03 }, 0.5)
        .to(hitsStage, { autoAlpha: 1, duration: 0.03 }, 0.52)
        .to(hitRows, { autoAlpha: 1, y: 0, duration: 0.05, stagger: 0.012 }, 0.52)
        .to(chapters[1], { autoAlpha: 0, y: -12, duration: 0.03 }, 0.48)
        .to(chapters[2], { autoAlpha: 1, y: 0, duration: 0.03 }, 0.52)
        .to(quote, { autoAlpha: 1, y: 0, duration: 0.06 }, 0.78)
        .to(chapters[2], { autoAlpha: 0, y: -12, duration: 0.03 }, 0.74)
        .to(chapters[3], { autoAlpha: 1, y: 0, duration: 0.03 }, 0.8)
        .to(progressBar, { scaleY: 1, duration: 1 }, 0);
    });

    mm.add('(max-width: 1080px), (prefers-reduced-motion: reduce)', () => {
      root.classList.add('is-static');
    });
  }

  /* ---------------------------------------------------------------- 接入 */
  const tabs = Array.from(document.querySelectorAll('[data-tab]'));
  const panels = Array.from(document.querySelectorAll('[data-panel]'));
  const indicator = document.querySelector('.tab-indicator');
  const tabsWrap = document.querySelector('.tabs');

  function positionIndicator() {
    if (!indicator) return;
    const active = tabs.find((tab) => tab.classList.contains('is-active'));
    if (!active) return;
    indicator.style.width = `${active.offsetWidth}px`;
    indicator.style.transform = `translateX(${active.offsetLeft}px)`;
  }

  function activateTab(name) {
    tabs.forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    panels.forEach((panel) => {
      const active = panel.dataset.panel === name;
      panel.classList.toggle('is-active', active);
      panel.hidden = !active;
    });
    positionIndicator();
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => activateTab(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      const index = tabs.indexOf(tab);
      let next = null;
      if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
      if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === 'Home') next = tabs[0];
      if (event.key === 'End') next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        next.focus();
        activateTab(next.dataset.tab);
      }
    });
  });

  if (tabsWrap) {
    window.addEventListener('resize', positionIndicator, { passive: true });
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(positionIndicator).catch(() => {});
    }
    positionIndicator();
    window.addEventListener('load', positionIndicator, { once: true });
  }

  /* ---------------------------------------------------------------- 复制 */
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const card = button.closest('.code-card');
      const code = card && card.querySelector('code');
      if (!code) return;
      const text = code.innerText;
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
        try {
          document.execCommand('copy');
        } catch (fallbackError) {
          /* 复制失败就算了，按钮会回到原文案 */
        }
        document.body.removeChild(area);
      }
      button.textContent = '已复制';
      button.classList.add('is-done');
      window.setTimeout(() => {
        button.textContent = '复制';
        button.classList.remove('is-done');
      }, 1600);
    });
  });

  /* ------------------------------------------------------------ 当前分区 */
  const navLinks = Array.from(document.querySelectorAll('.nav a'));
  const sections = navLinks
    .map((link) => document.querySelector(link.getAttribute('href')))
    .filter(Boolean);

  if ('IntersectionObserver' in window && sections.length) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          navLinks.forEach((link) => {
            link.classList.toggle(
              'is-current',
              link.getAttribute('href') === `#${entry.target.id}`
            );
          });
        });
      },
      { rootMargin: '-42% 0px -52% 0px', threshold: 0 }
    );
    sections.forEach((section) => observer.observe(section));
  }
})();
