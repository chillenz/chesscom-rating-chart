const box = document.querySelector('.placeholder-box');
const img = document.querySelector('.placeholder-img');

console.log("TEST")
box.addEventListener('mousemove', (e) => {
  const rect = box.getBoundingClientRect();
  const x = e.clientX - rect.left; 
  const y = e.clientY - rect.top;  
  const centerX = rect.width / 2;
  const centerY = rect.height / 2;

  const rotateX = ((y - centerY) / centerY) * 10; 
  const rotateY = ((x - centerX) / centerX) * -10; 

  img.style.transform = `rotateX(${rotateX}deg) rotateY(${rotateY}deg)`;
});

box.addEventListener('mouseleave', () => {
  img.style.transform = `rotateX(0deg) rotateY(0deg)`;
});