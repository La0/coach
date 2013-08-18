
$(function(){
  // Modals show
  $('.modal-action').click(load_modal);

  // Roles custom
  $(document).on('click', 'div.roles button', function(){
    if($(this).hasClass('disabled')) return false;
    $(this).parents('div.roles').find('input.role_value').val($(this).val());
  });
});

function submit_form(evt){
  evt.preventDefault();

  // Use datas from form
  data = {}
  $(this).find(':input').each(function(){
    v = $(this).val();
    n = this.getAttribute('name');
    if(n && v)
      data[n] = v;
  });

  // Send data
  load_box(this.getAttribute('action'), 'POST', data);
  return false;
}

// Load & Display a json "box"
var modal = null;
function load_box(url, method, data){
  if(modal == null)
    $('body').modalmanager('loading'); // loading state

  $.ajax({
    url : url,
    method : method,
    data : data ? data : null,
    dataType : 'json',
    success : function(data){
      // Build a new modal
      modal = $(data.html).modal({
        show : true,
        replace : true,
      });

      // Status
      // TODO: some action here
      status = data.status;
      console.log("Modal Status = "+status);

      // Trigger forms
      modal.find('form').on('submit', submit_form);
    },
  });
}

// Init methos, used from click
function load_modal(evt){
  // Get url
  var url = this.getAttribute('href');
  if(!url){
    console.error("No url for modal");
    return false;
  }
  evt.preventDefault();
  load_box(url, 'GET');
  return false;
}
