const cards = [...document.querySelectorAll('.document-card')]
const emptyState = document.querySelector('#emptyState')
const searchInput = document.querySelector('#searchInput')
const toast = document.querySelector('#toast')
const refreshBtn = document.querySelector('#refreshBtn')
let filter = 'all'
let category = 'all'
let toastTimer

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

function showToast(message) {
  toast.textContent = message
  toast.classList.add('show')
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200)
}

document.querySelectorAll('.segment').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelector('.segment.active').classList.remove('active')
    button.classList.add('active')
    filter = button.dataset.filter
    renderDocuments()
  })
})

document.querySelectorAll('.quick-card').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelector('.quick-card.selected').classList.remove('selected')
    button.classList.add('selected')
    category = button.dataset.category
    filter = 'all'
    document.querySelector('.segment.active').classList.remove('active')
    document.querySelector('[data-filter="all"]').classList.add('active')
    renderDocuments()
  })
})

searchInput.addEventListener('input', renderDocuments)
document.querySelector('#favoritesBtn').addEventListener('click', () => document.querySelector('[data-filter="favorite"]').click())

document.querySelectorAll('.favorite').forEach((button) => {
  button.addEventListener('click', () => {
    const active = button.classList.toggle('active')
    button.textContent = active ? '★' : '☆'
    button.setAttribute('aria-label', active ? 'Retirer des favoris' : 'Ajouter aux favoris')
    button.closest('.document-card').dataset.filter = active ? 'favorite' : 'recent'
    showToast(active ? 'Document ajouté à tes favoris' : 'Document retiré de tes favoris')
  })
})

document.querySelectorAll('.open-doc').forEach((button) => {
  button.addEventListener('click', () => showToast('Ouverture du document…'))
})

refreshBtn.addEventListener('click', () => {
  refreshBtn.classList.remove('spin')
  void refreshBtn.offsetWidth
  refreshBtn.classList.add('spin')
  setTimeout(() => { refreshBtn.textContent = '✓'; showToast('Documents synchronisés'); setTimeout(() => { refreshBtn.textContent = '↻' }, 1400) }, 600)
})
