const cards = [...document.querySelectorAll('.document-card')]
const emptyState = document.querySelector('#emptyState')
const searchInput = document.querySelector('#searchInput')
const toast = document.querySelector('#toast')
const refreshBtn = document.querySelector('#refreshBtn')
const sidebar = document.querySelector('.sidebar')
let filter = 'all'
let category = 'all'
let toastTimer

function showToast(message) {
  toast.textContent = message
  toast.classList.add('show')
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200)
}

function renderDocuments() {
  const query = searchInput.value.trim().toLowerCase()
  let visible = 0
  cards.forEach((card) => {
    const matchesFilter = filter === 'all' || card.dataset.filter === filter
    const matchesCategory = category === 'all' || card.dataset.category === category
    const matchesSearch = card.textContent.toLowerCase().includes(query)
    const show = matchesFilter && matchesCategory && matchesSearch
    card.hidden = !show
    if (show) visible += 1
  })
  emptyState.hidden = visible !== 0
}

function setFilter(nextFilter) {
  filter = nextFilter
  document.querySelectorAll('.segment').forEach((button) => {
    button.classList.toggle('active', button.dataset.filter === nextFilter)
  })
  renderDocuments()
}

document.querySelectorAll('.segment').forEach((button) => {
  button.addEventListener('click', () => setFilter(button.dataset.filter))
})

document.querySelectorAll('.quick-card').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.quick-card').forEach((item) => item.classList.remove('selected'))
    button.classList.add('selected')
    category = button.dataset.category
    setFilter('all')
    showToast(category === 'all' ? 'Tous les documents affichés' : `${button.querySelector('strong').textContent} affichés`)
  })
})

searchInput.addEventListener('input', renderDocuments)
document.querySelector('#favoritesBtn').addEventListener('click', () => setFilter('favorite'))

document.querySelectorAll('.favorite').forEach((button) => {
  button.addEventListener('click', (event) => {
    event.stopPropagation()
    const active = button.classList.toggle('active')
    button.textContent = active ? '★' : '☆'
    button.setAttribute('aria-label', active ? 'Retirer des favoris' : 'Ajouter aux favoris')
    button.closest('.document-card').dataset.filter = active ? 'favorite' : 'recent'
    showToast(active ? 'Document ajouté à tes favoris' : 'Document retiré de tes favoris')
    if (filter === 'favorite') renderDocuments()
  })
})

document.querySelectorAll('.open-doc').forEach((button) => {
  button.addEventListener('click', () => {
    const title = button.closest('.document-card').querySelector('h3').textContent
    showToast(`Ouverture de « ${title} »`)
  })
})

refreshBtn.addEventListener('click', () => {
  refreshBtn.classList.remove('spin')
  void refreshBtn.offsetWidth
  refreshBtn.classList.add('spin')
  refreshBtn.disabled = true
  setTimeout(() => {
    refreshBtn.disabled = false
    refreshBtn.textContent = '✓'
    showToast('Documents synchronisés')
    setTimeout(() => { refreshBtn.textContent = '↻' }, 1400)
  }, 600)
})

document.querySelectorAll('.nav-item').forEach((item) => {
  item.addEventListener('click', (event) => {
    event.preventDefault()
    document.querySelectorAll('.nav-item').forEach((navItem) => navItem.classList.remove('active'))
    item.classList.add('active')
    showToast(`${item.textContent.trim().replace(/\s+/g, ' ')} sélectionné`)
    sidebar.classList.remove('open')
  })
})

document.querySelector('.dots').addEventListener('click', () => showToast('Menu du profil bientôt disponible'))
document.querySelector('.icon-button').addEventListener('click', () => showToast('Aucune nouvelle notification'))
document.querySelector('.help').addEventListener('click', () => showToast('Besoin d’aide ? Consulte la FAQ Pronote Blocus'))
document.querySelector('.mobile-menu').addEventListener('click', () => sidebar.classList.toggle('open'))

document.addEventListener('keydown', (event) => {
  if (event.key === '/' && document.activeElement !== searchInput) {
    event.preventDefault()
    searchInput.focus()
  }
  if (event.key === 'Escape') sidebar.classList.remove('open')
})

renderDocuments()
