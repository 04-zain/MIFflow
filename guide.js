const menu = document.querySelector('.menu');
const library = document.querySelector('.library');
const navigation = document.querySelector('#contents');
const narrow = window.matchMedia('(max-width: 52rem)');
const outline = document.querySelector('.outline');

function closeMenu() {
  menu.setAttribute('aria-expanded', 'false');
  library.classList.remove('expanded');
}

menu.addEventListener('click', () => {
  const open = library.classList.toggle('expanded');
  menu.setAttribute('aria-expanded', String(open));
  if (open) (navigation.querySelector('[aria-current]') || navigation.querySelector('a')).focus();
});
narrow.addEventListener('change', closeMenu);
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (library.classList.contains('expanded')) {
    closeMenu();
    menu.focus();
  } else if (outline?.open) {
    outline.open = false;
    outline.querySelector('summary').focus();
  }
});
document.addEventListener('click', event => {
  if (!library.contains(event.target)) closeMenu();
  if (outline && (!outline.contains(event.target) || event.target.closest('a'))) outline.open = false;
});
document.documentElement.classList.add('interactive');
const current = navigation.querySelector('[aria-current]');
if (!narrow.matches && current) {
  const distance = current.getBoundingClientRect().bottom - navigation.getBoundingClientRect().bottom;
  if (distance > 0) navigation.scrollTop += distance + 30;
}
