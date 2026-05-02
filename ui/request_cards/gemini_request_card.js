function normalizeRequestMethod(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function isReadOnlyEvent(event, helpers) {
  return helpers?.readOnly === true
    || event?.replay === true
    || event?.event === 'approval_decision'
    || typeof event?.status === 'string';
}

function readOnlyStatusLabel(event, fallback = 'Recorded response') {
  const parts = [];
  if (typeof event?.status === 'string' && event.status.trim()) parts.push(event.status.trim());
  if (typeof event?.decision === 'string' && event.decision.trim()) parts.push(event.decision.trim());
  if (typeof event?.result?.action === 'string' && event.result.action.trim()) parts.push(event.result.action.trim());
  return parts.length ? `${fallback}: ${parts.join(' / ')}` : fallback;
}

function actionLabel(action) {
  switch (String(action || '').trim()) {
    case 'accept':
      return 'Approved once';
    case 'acceptForSession':
      return 'Approved for rest of session';
    case 'decline':
      return 'Rejected';
    case 'cancel':
      return 'Cancelled';
    default:
      return 'Recorded response';
  }
}

function createFeedbackNode(body) {
  const feedback = document.createElement('div');
  feedback.className = 'approval-feedback';
  body.append(feedback);
  return feedback;
}

function setFeedback(node, message, isError = false) {
  if (!(node instanceof HTMLElement)) return;
  node.textContent = message || '';
  node.style.color = isError ? '#c62828' : '';
}

async function trySubmit(helpers, result, meta, feedbackNode, pendingMessage = '') {
  if (pendingMessage) {
    setFeedback(feedbackNode, pendingMessage, false);
  }
  const outcome = await helpers.submitResult(result, meta);
  if (!outcome || outcome.ok === false) {
    const message = outcome?.response?.error || 'Request failed';
    setFeedback(feedbackNode, message, true);
    return false;
  }
  return true;
}

function appendValueRow(container, label, value) {
  if (!(container instanceof HTMLElement)) return;
  if (value === null || value === undefined || value === '') return;
  const row = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = `${label}: `;
  row.append(strong, document.createTextNode(String(value)));
  container.append(row);
}

function renderMarkdownNode(container, text, helpers, extraClass = '') {
  if (!(container instanceof HTMLElement)) return;
  if (typeof helpers?.renderMarkdown === 'function') {
    helpers.renderMarkdown(container, text, extraClass);
    return;
  }
  if (typeof extraClass === 'string' && extraClass.trim()) {
    container.className = extraClass.trim();
  }
  container.textContent = String(text || '');
}

function appendMarkdownRow(container, label, value, helpers) {
  if (!(container instanceof HTMLElement)) return;
  if (value === null || value === undefined || value === '') return;
  const row = document.createElement('div');
  const title = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = `${label}:`;
  title.append(strong);
  const content = document.createElement('div');
  renderMarkdownNode(content, String(value), helpers);
  row.append(title, content);
  container.append(row);
}

function addJsonDetails(body, label, value) {
  if (!(body instanceof HTMLElement)) return;
  if (value === null || value === undefined || value === '') return;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = label;
  const pre = document.createElement('pre');
  pre.className = 'approval-extra';
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(summary, pre);
  body.append(details);
}

function appendStringList(body, label, values) {
  if (!(body instanceof HTMLElement)) return;
  if (!Array.isArray(values) || !values.length) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'approval-summary';
  const title = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = `${label}:`;
  title.append(strong);
  wrapper.append(title);
  values.forEach((value) => {
    if (typeof value !== 'string' || !value.trim()) return;
    const row = document.createElement('div');
    row.textContent = value;
    wrapper.append(row);
  });
  body.append(wrapper);
}

function normalizeApprovalPolicyLabel(value) {
  switch (String(value || '').trim()) {
    case 'always_approve':
      return 'Always approve';
    case 'ask':
      return 'Ask';
    case 'ask_clear':
      return 'Ask ✅';
    case 'always_reject':
      return 'Always reject';
    default:
      return '';
  }
}

function renderPermissionCard(body, event, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  const diffText = typeof payload.diff === 'string' ? payload.diff : (typeof requestParams.diff === 'string' ? requestParams.diff : '');
  const filePath = typeof payload.path === 'string' ? payload.path : (typeof requestParams.path === 'string' ? requestParams.path : '');
  const canApproveAll = payload.can_offer_session_approval === true
    || (Array.isArray(requestParams.availableDecisions) && requestParams.availableDecisions.includes('acceptForSession'));
  const approvalPolicyLabel = normalizeApprovalPolicyLabel(requestParams.currentApprovalPolicy);

  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendValueRow(summary, 'Kind', payload.kind || requestParams.kind || '');
  appendValueRow(summary, 'Tool', payload.tool_name || requestParams.tool_name || '');
  appendMarkdownRow(summary, 'Request', payload.message || requestParams.intention || requestParams.message || '', helpers);
  appendValueRow(summary, 'Approval', approvalPolicyLabel);
  appendValueRow(
    summary,
    'Command',
    Array.isArray(payload.command)
      ? payload.command.join(' ')
      : (payload.command || requestParams.command || ''),
  );
  appendValueRow(summary, 'Path', filePath);
  appendValueRow(summary, 'CWD', payload.cwd || requestParams.cwd || '');
  body.append(summary);

  const warning = requestParams.warning || payload.warning;
  if (typeof warning === 'string' && warning.trim()) {
    const warningNode = document.createElement('div');
    renderMarkdownNode(warningNode, warning, helpers, 'approval-feedback');
    body.append(warningNode);
  }

  if (diffText && typeof helpers?.formatDiff === 'function') {
    const diffBlock = document.createElement('div');
    diffBlock.className = 'diff-block';
    diffBlock.innerHTML = helpers.formatDiff(diffText, filePath || null);
    body.append(diffBlock);
  }

  appendStringList(body, 'Possible paths', requestParams.possible_paths || payload.possible_paths);
  addJsonDetails(body, 'Tool arguments', payload.arguments ?? requestParams.arguments ?? null);
  addJsonDetails(body, 'Request details', requestParams.request ?? payload.request ?? null);
  addJsonDetails(body, 'Change preview', payload.changes ?? requestParams.changes ?? null);

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    const action = event?.result?.action || event?.decision || event?.status;
    const label = actionLabel(action);
    setFeedback(feedback, label === 'Recorded response' ? readOnlyStatusLabel(event) : label, false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }

  const actions = document.createElement('div');
  actions.className = 'actions';

  const approveOnceButton = document.createElement('button');
  approveOnceButton.className = 'btn tiny approve';
  approveOnceButton.textContent = 'Approve once';
  approveOnceButton.addEventListener('click', async () => {
    await trySubmit(helpers, { decision: 'accept' }, { diff: diffText, path: filePath }, feedback, 'Sending response…');
  });
  actions.append(approveOnceButton);

  if (canApproveAll) {
    const approveAllButton = document.createElement('button');
    approveAllButton.className = 'btn tiny approve';
    approveAllButton.textContent = 'Approve all';
    approveAllButton.addEventListener('click', async () => {
      await trySubmit(helpers, { decision: 'acceptForSession' }, { diff: diffText, path: filePath }, feedback, 'Sending response…');
    });
    actions.append(approveAllButton);
  }

  const rejectButton = document.createElement('button');
  rejectButton.className = 'btn tiny decline';
  rejectButton.textContent = 'Reject';
  rejectButton.addEventListener('click', async () => {
    await trySubmit(helpers, { decision: 'decline' }, { diff: diffText, path: filePath }, feedback, 'Sending response…');
  });
  actions.append(rejectButton);

  body.append(actions);
}

export async function renderRequestCard(ctx = {}) {
  const event = ctx.event && typeof ctx.event === 'object' ? ctx.event : {};
  const helpers = ctx.helpers && typeof ctx.helpers === 'object' ? ctx.helpers : {};
  const body = ctx.body;
  if (!(body instanceof HTMLElement)) return false;

  const requestMethod = normalizeRequestMethod(event.request_method || event.requestMethod);
  if (requestMethod !== 'gemini-acp/permission') {
    return false;
  }

  renderPermissionCard(body, event, helpers);
  return true;
}
