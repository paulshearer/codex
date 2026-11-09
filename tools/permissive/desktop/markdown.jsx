import React, { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { tableWorkbook } from './table-export.mjs';

function plain(node) { return node.type === 'text' ? node.value : (node.children || []).map(plain).join(''); }
function Table({ node, children }) {
  const [open, setOpen] = useState(false), [error, setError] = useState('');
  const dialog = useRef(null), trigger = useRef(null);
  const headerRow = node.children.find(child => child.tagName === 'thead')?.children.find(child => child.tagName === 'tr');
  const cells = (headerRow?.children || []).filter(child => child.tagName === 'th');
  const headers = cells.map(plain), alignments = cells.map(child => child.properties?.align || 'left');
  const rows = (node.children.find(child => child.tagName === 'tbody')?.children || []).filter(child => child.tagName === 'tr')
    .map(row => row.children.filter(child => child.tagName === 'td').map(plain));
  const summary = `${rows.length} ${rows.length === 1 ? 'row' : 'rows'} × ${headers.length} columns`;
  async function exportTable() {
    try { setError(''); await window.permissive.saveTable(Array.from(tableWorkbook({ headers, rows, alignments }))); }
    catch (error) { setError(error.message); }
  }
  function close() { setOpen(false); trigger.current?.focus(); }
  function keyboard(event) {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const buttons = [...dialog.current.querySelectorAll('button,a[href]')];
      const first = buttons[0], last = buttons.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  return <>
    <button ref={trigger} className="table-popout-button" aria-label={`Open table: ${summary}`} onClick={() => setOpen(true)}>
      <span aria-hidden="true">▦</span><span><strong>Table</strong><small>{summary} · {headers.slice(0, 3).join(', ')}</small></span>
    </button>
    {open && createPortal(<div className="table-modal-backdrop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
      <div ref={dialog} className="table-modal-panel" role="dialog" aria-modal="true" aria-label="Table" onKeyDown={keyboard}>
        <header><div><h2>Table</h2><small>{summary}</small></div><div className="table-modal-actions"><button onClick={exportTable}>Export as Excel</button><button autoFocus onClick={close}>Close table</button></div></header>
        {error && <p role="alert" className="error">{error}</p>}
        <div className="rich-table-scroll"><table className="rich-markdown-table">{children}</table></div>
      </div>
    </div>, document.body)}
  </>;
}
function Cell({ node, children, header }) {
  const Tag = header ? 'th' : 'td';
  return <Tag scope={header ? 'col' : undefined} className={`align-${node.properties?.align || 'left'}`}>{children}</Tag>;
}
const components = { table: Table, th: props => <Cell {...props} header />, td: Cell,
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" /> };
export function Markdown({ children }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>{children}</ReactMarkdown></div>;
}
