const tasks = [...document.querySelectorAll('.task')]
const emptyState = document.querySelector('#emptyState')
const searchInput = document.querySelector('#searchInput')
const refreshBtn = document.querySelector('#refreshBtn')
let filter = 'all'

function renderTasks() {
  const query = searchInput.value.trim().toLowerCase()
  let visible = 0
  tasks.forEach((task) => {
    const matchesFilter = filter === 'all' || task.dataset.level === filter
    const matchesSearch = task.textContent.toLowerCase().includes(query)
    const show = matchesFilter && matchesSearch
    task.hidden = !show
    if (show) visible += 1
  })
  emptyState.hidden = visible !== 0
}

document.querySelectorAll('.segment').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelector('.segment.active').classList.remove('active')
    button.classList.add('active')
    filter = button.dataset.filter
    renderTasks()
  })
})
searchInput.addEventListener('input', renderTasks)
tasks.forEach((task) => {
  task.querySelector('.check').addEventListener('click', () => task.classList.toggle('done'))
})
refreshBtn.addEventListener('click', () => {
  refreshBtn.classList.remove('spin')
  void refreshBtn.offsetWidth
  refreshBtn.classList.add('spin')
  setTimeout(() => { refreshBtn.textContent = '✓'; setTimeout(() => { refreshBtn.textContent = '↻' }, 1400) }, 600)
})
document.querySelector('#showAll').addEventListener('click', () => {
  document.querySelector('[data-filter="all"]').click()
  document.querySelector('#devoirs').scrollIntoView({ behavior: 'smooth' })
})
