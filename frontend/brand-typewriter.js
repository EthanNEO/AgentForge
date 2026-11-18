// A restored page keeps its DOM and finished CSS animation.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;

  const print = document.querySelector(".brand-typewriter-print");
  for (const animation of print.getAnimations()) {
    animation.currentTime = 0;
    animation.play();
  }
});
