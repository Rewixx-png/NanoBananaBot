const BRIDGE_URL = 'http://94.156.179.21:7432';
const BRIDGE_SECRET = 'nanohatani_figma_bridge';
const POLL_INTERVAL = 2500;

figma.showUI(__html__, { visible: false, width: 0, height: 0 });

figma.ui.onmessage = async (msg) => {
  if (msg.type === 'poll-result' && msg.session_id) {
    await buildDesign(msg.spec, msg.session_id);
  }
};

function schedulePoll() {
  setTimeout(() => {
    figma.ui.postMessage({
      type: 'poll',
      url: BRIDGE_URL + '/figma/poll',
      secret: BRIDGE_SECRET,
    });
  }, POLL_INTERVAL);
}

figma.ui.onmessage = async (msg) => {
  if (msg.type === 'poll-result') {
    schedulePoll();
    if (msg.session_id) {
      await buildDesign(msg.spec, msg.session_id);
    }
  } else if (msg.type === 'poll-done') {
    schedulePoll();
  }
};

async function buildDesign(spec, session_id) {
  try {
    const fonts = new Set();
    fonts.add(JSON.stringify({ family: 'Inter', style: 'Regular' }));
    fonts.add(JSON.stringify({ family: 'Inter', style: 'Bold' }));
    for (const node of (spec.nodes || [])) {
      if (node.type === 'TEXT') {
        const family = node.fontFamily || 'Inter';
        const style = node.fontStyle || 'Regular';
        fonts.add(JSON.stringify({ family, style }));
      }
    }
    for (const f of fonts) {
      try { await figma.loadFontAsync(JSON.parse(f)); } catch (_) {}
    }

    const frame = figma.createFrame();
    frame.name = spec.frame?.name || 'Bot Design';
    frame.resize(spec.frame?.width || 1280, spec.frame?.height || 720);
    frame.clipsContent = true;

    const bg = spec.frame?.backgroundColor;
    if (bg) {
      frame.fills = [{ type: 'SOLID', color: { r: bg.r ?? 1, g: bg.g ?? 1, b: bg.b ?? 1 } }];
    } else {
      frame.fills = [{ type: 'SOLID', color: { r: 1, g: 1, b: 1 } }];
    }

    for (const node of (spec.nodes || [])) {
      if (node.type === 'RECTANGLE' || node.type === 'RECT') {
        const rect = figma.createRectangle();
        rect.x = node.x ?? 0;
        rect.y = node.y ?? 0;
        rect.resize(node.width ?? 100, node.height ?? 50);
        if (node.fill) {
          rect.fills = [{ type: 'SOLID', color: { r: node.fill.r ?? 0, g: node.fill.g ?? 0, b: node.fill.b ?? 0 }, opacity: node.fill.a ?? 1 }];
        } else {
          rect.fills = [];
        }
        if (node.cornerRadius != null) rect.cornerRadius = node.cornerRadius;
        if (node.stroke) {
          rect.strokes = [{ type: 'SOLID', color: { r: node.stroke.r ?? 0, g: node.stroke.g ?? 0, b: node.stroke.b ?? 0 } }];
          rect.strokeWeight = node.strokeWeight ?? 1;
        }
        if (node.opacity != null) rect.opacity = node.opacity;
        if (node.name) rect.name = node.name;
        frame.appendChild(rect);

      } else if (node.type === 'ELLIPSE' || node.type === 'CIRCLE') {
        const ellipse = figma.createEllipse();
        ellipse.x = node.x ?? 0;
        ellipse.y = node.y ?? 0;
        ellipse.resize(node.width ?? 50, node.height ?? 50);
        if (node.fill) {
          ellipse.fills = [{ type: 'SOLID', color: { r: node.fill.r ?? 0, g: node.fill.g ?? 0, b: node.fill.b ?? 0 }, opacity: node.fill.a ?? 1 }];
        } else {
          ellipse.fills = [];
        }
        if (node.opacity != null) ellipse.opacity = node.opacity;
        if (node.name) ellipse.name = node.name;
        frame.appendChild(ellipse);

      } else if (node.type === 'TEXT') {
        const text = figma.createText();
        text.x = node.x ?? 0;
        text.y = node.y ?? 0;
        try {
          text.fontName = { family: node.fontFamily || 'Inter', style: node.fontStyle || 'Regular' };
        } catch (_) {
          text.fontName = { family: 'Inter', style: 'Regular' };
        }
        text.fontSize = node.fontSize ?? 16;
        text.characters = node.content ?? '';
        if (node.color) {
          text.fills = [{ type: 'SOLID', color: { r: node.color.r ?? 0, g: node.color.g ?? 0, b: node.color.b ?? 0 } }];
        }
        if (node.width) text.resize(node.width, text.height);
        if (node.textAlignHorizontal) text.textAlignHorizontal = node.textAlignHorizontal;
        if (node.opacity != null) text.opacity = node.opacity;
        if (node.name) text.name = node.name;
        frame.appendChild(text);

      } else if (node.type === 'LINE') {
        const line = figma.createLine();
        line.x = node.x ?? 0;
        line.y = node.y ?? 0;
        line.resize(node.width ?? 100, 0);
        if (node.stroke) {
          line.strokes = [{ type: 'SOLID', color: { r: node.stroke.r ?? 0, g: node.stroke.g ?? 0, b: node.stroke.b ?? 0 } }];
          line.strokeWeight = node.strokeWeight ?? 1;
        }
        if (node.name) line.name = node.name;
        frame.appendChild(line);
      }
    }

    figma.currentPage.appendChild(frame);
    figma.viewport.scrollAndZoomIntoView([frame]);

    figma.ui.postMessage({
      type: 'done',
      url: BRIDGE_URL + '/figma/done',
      secret: BRIDGE_SECRET,
      session_id,
      node_id: frame.id,
    });
  } catch (err) {
    figma.ui.postMessage({
      type: 'done',
      url: BRIDGE_URL + '/figma/done',
      secret: BRIDGE_SECRET,
      session_id,
      node_id: '',
      error: String(err),
    });
  }
}

schedulePoll();
