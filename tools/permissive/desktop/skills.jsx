import React, { useEffect, useState } from 'react';

const api = window.permissive;
export function SkillList({ catalog, selected, onSelect, onPreview, maxSelected = Infinity }) {
  const [search, setSearch] = useState(''), [category, setCategory] = useState('');
  const categories = [...new Set(catalog.map(skill => skill.category || 'Uncategorized'))].sort();
  const activeCategory = categories.includes(category) ? category : '';
  const visible = catalog.filter(skill => (!activeCategory || (skill.category || 'Uncategorized') === activeCategory)
    && `${skill.name} ${skill.description} ${skill.path} ${skill.category || ''}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <div className="options"><label>Find skills<input aria-label="Find skills" type="search" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <label>Category<select aria-label="Filter by category" value={activeCategory} onChange={event => setCategory(event.target.value)}><option value="">All categories</option>{categories.map(name => <option key={name}>{name}</option>)}</select></label></div>
    <div className="skill-list">{categories.filter(name => visible.some(skill => (skill.category || 'Uncategorized') === name)).map(name => {
      const group = visible.filter(skill => (skill.category || 'Uncategorized') === name);
      const groupSelection = [...new Set([...selected, ...group.map(skill => skill.path)])];
      return <details className="skill-group" open key={name}><summary>{name} ({group.length})</summary>
        <button type="button" className="group-select" disabled={groupSelection.length > maxSelected} title={groupSelection.length > maxSelected ? `This would select ${groupSelection.length} skills. Choose up to ${maxSelected} individually.` : undefined} onClick={() => onSelect(groupSelection)}>Select all {group.length} skills in {name}</button>
        {group.map(skill => <div className="skill-row" key={skill.path}><label className="skill-choice">
          <input type="checkbox" checked={selected.includes(skill.path)} disabled={!selected.includes(skill.path) && selected.length >= maxSelected} onChange={event => onSelect(event.target.checked ? [...selected, skill.path] : selected.filter(path => path !== skill.path))} />
          <span><strong>{skill.name}</strong><small>{skill.description}</small><small className="skill-path">{skill.path}</small></span>
        </label><button type="button" onClick={() => onPreview(skill.path)} aria-label={`View skill ${skill.name}`}>View</button></div>)}
      </details>;
    })}{!visible.length && <p className="muted">No skills match. Add SKILL.md files or import a skill bundle.</p>}</div>
  </>;
}

export function SkillPreview({ path, onClose }) {
  const [document, setDocument] = useState(null), [error, setError] = useState('');
  useEffect(() => {
    let live = true; setDocument(null); setError('');
    api.skillDocument(path).then(value => { if (live) setDocument(value); }).catch(error => { if (live) setError(error.message); });
    return () => { live = false; };
  }, [path]);
  return <div className="skill-preview"><div className="settings-section-head"><h2>{document?.name || 'Skill preview'}</h2><button type="button" onClick={onClose}>Close preview</button></div>
    {error ? <p role="alert" className="error">{error}</p> : document ? <><p className="muted">{document.description}</p><p className="skill-path">{document.path}</p><pre>{document.content}</pre></> : <p role="status">Loading skill…</p>}
  </div>;
}

export function SkillLibrary({ workspace, onAssign, onClose }) {
  const [catalog, setCatalog] = useState([]), [selected, setSelected] = useState([]), [preview, setPreview] = useState(null);
  const [category, setCategory] = useState(''), [error, setError] = useState(''), [status, setStatus] = useState(''), [busy, setBusy] = useState(false);
  async function refresh() { setCatalog((await api.skills(undefined, workspace?.workingDirectory)).data); }
  useEffect(() => { refresh().catch(error => setError(error.message)); }, [workspace?.id, workspace?.workingDirectory]);
  async function perform(action) {
    setBusy(true); setError(''); setStatus('');
    try { await action(); } catch (error) { setError(error.message); } finally { setBusy(false); }
  }
  async function add() {
    const paths = await api.chooseSkills(); if (!paths.length) return;
    await api.registerSkills(paths); await refresh(); setStatus(`${paths.length} skill${paths.length === 1 ? '' : 's'} added.`);
  }
  async function importBundle() {
    const result = await api.importSkills(); if (!result) return;
    await refresh(); setSelected(result.data.map(skill => skill.path)); setStatus(`${result.data.length} skill${result.data.length === 1 ? '' : 's'} imported.`);
  }
  async function exportBundle() { const file = await api.exportSkills(selected); if (file) setStatus(`Skills exported to ${file}`); }
  async function categorize() { await api.skillCategory(selected, category); await refresh(); setStatus('Category saved.'); }
  const categories = [...new Set(catalog.map(skill => skill.category || 'Uncategorized'))].sort();
  return <div className="workspace-editor skill-library"><header><div><h1>Skills</h1><p className="muted">Browse skill instructions and group them by category.</p></div><button onClick={onClose}>Close library</button></header>
    {error && <div className="error" role="alert">{error}</div>}{status && <p role="status">{status}</p>}
    <div className="skill-toolbar"><button disabled={busy} onClick={() => perform(add)}>Add skill files…</button><button disabled={busy} onClick={() => perform(importBundle)}>Import skills…</button>
      <button disabled={busy || !selected.length || selected.length > 32} onClick={() => perform(exportBundle)}>Export selected skills…</button></div>
    <SkillList catalog={catalog} selected={selected} onSelect={setSelected} onPreview={setPreview} />
    <p className="muted">{selected.length} selected. Import/export includes SKILL.md, supporting files, and categories. Export up to 32 skills per bundle.</p>
    <div className="category-editor"><label>Set category<input aria-label="Set category" list="skill-categories" maxLength={120} value={category} placeholder="For example, Development" onChange={event => setCategory(event.target.value)} /></label>
      <datalist id="skill-categories">{categories.map(name => <option key={name} value={name} />)}</datalist><button disabled={busy || !selected.length} onClick={() => perform(categorize)}>Apply category</button><button disabled={busy || !selected.length} onClick={() => setSelected([])}>Clear selection</button></div>
    {workspace && <button disabled={busy || !selected.length || new Set([...workspace.skills, ...selected]).size > 32} onClick={() => perform(async () => { await onAssign(selected); setStatus(`Skills assigned to ${workspace.name}.`); })}>Assign selected to {workspace.name}</button>}
    {preview && <SkillPreview path={preview} onClose={() => setPreview(null)} />}
  </div>;
}
